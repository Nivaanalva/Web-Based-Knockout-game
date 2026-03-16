"""
Knockout Game Server
FastAPI + WebSockets backend for 3D browser-based Knockout game
Run: uvicorn server:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import math
import random
import time
import uuid
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Knockout Game Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Constants ────────────────────────────────────────────────────────────────
PLATFORM_SIZE     = 24.0      # square platform width
PLATFORM_MIN      = 8.0       # minimum platform size
PLATFORM_SHRINK   = 2.0       # shrink per elimination
FALL_THRESHOLD    = -6.0
TICK_RATE         = 0.05
PUNCH_RANGE       = 3.0
PUNCH_FORCE_MIN   = 8.0
PUNCH_FORCE_MAX   = 28.0
PUNCH_COOLDOWN    = 1.0
GRAVITY           = -20.0
GROUND_Y          = 0.0
PLAYER_SPEED      = 3.5       # slower, less sensitive
JUMP_VEL          = 9.0
LOBBY_MAX         = 9
LOBBY_START_MIN   = 1
COUNTDOWN_SECS    = 5
AI_TICK           = 0.25

COLORS = ["#FF6B6B","#FFD93D","#6BCB77","#4D96FF",
          "#C77DFF","#FF9A3C","#00C9A7","#FF61A6"]

# ─── Global State ─────────────────────────────────────────────────────────────
lobbies:    Dict[str, "Lobby"]    = {}
leaderboard: Dict[str, int]       = {}   # username -> total wins
accounts:   Dict[str, dict]       = {}   # username -> {password, wins, games}

ACCOUNTS_FILE = "accounts.json"

def load_accounts():
    global accounts, leaderboard
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            accounts = json.load(f)
            leaderboard = {u: d.get("wins", 0) for u, d in accounts.items()}
            print(f"Loaded {len(accounts)} accounts from file")
    except FileNotFoundError:
        accounts = {}
        print("No accounts file found, starting fresh")

def save_accounts():
    try:
        with open(ACCOUNTS_FILE, "w") as f:
            json.dump(accounts, f)
    except Exception as e:
        print(f"Error saving accounts: {e}")

load_accounts()

# ─── Data Classes ─────────────────────────────────────────────────────────────
class Player:
    def __init__(self, pid: str, name: str, color: str, is_ai: bool = False):
        self.id          = pid
        self.name        = name
        self.color       = color
        self.is_ai       = is_ai
        self.ws: Optional[WebSocket] = None

        # Physics state
        self.x  = random.uniform(-8, 8)
        self.y  = GROUND_Y
        self.z  = random.uniform(-8, 8)
        self.vx = 0.0
        self.vy = 0.0
        self.vz = 0.0
        self.rot_y = 0.0  # facing angle

        self.alive         = True
        self.on_ground     = False
        self.last_punch    = 0.0
        self.kills         = 0
        self.deaths        = 0

        # AI-specific
        self.ai_target: Optional[str] = None
        self.ai_next_decision = 0.0

    def to_dict(self):
        return {
            "id":    self.id,
            "name":  self.name,
            "color": self.color,
            "is_ai": self.is_ai,
            "x": self.x, "y": self.y, "z": self.z,
            "vx": self.vx, "vy": self.vy, "vz": self.vz,
            "rot_y": self.rot_y,
            "alive":  self.alive,
            "kills":  self.kills,
            "deaths": self.deaths,
        }

    def spawn(self, index: int, total: int):
        """Spawn evenly around the square"""
        angle = (2 * math.pi * index) / max(total, 1)
        r = (PLATFORM_SIZE / 2) * 0.55
        self.x  = r * math.cos(angle)
        self.y  = GROUND_Y
        self.z  = r * math.sin(angle)
        self.vx = self.vy = self.vz = 0.0
        self.alive     = True
        self.on_ground = True


class Lobby:
    def __init__(self, lobby_id: str, host_name: str):
        self.id          = lobby_id
        self.host_name   = host_name
        self.players:    Dict[str, Player] = {}
        self.chat:       List[dict]        = []
        self.state       = "waiting"   # waiting | countdown | playing | ended
        self.countdown   = COUNTDOWN_SECS
        self.winner:     Optional[str] = None
        self.platform_size = PLATFORM_SIZE
        self._task:      Optional[asyncio.Task] = None
        self._last_tick  = time.time()

    # ── Player management ─────────────────────────────────────────────────────
    def add_player(self, player: Player):
        self.players[player.id] = player

    def remove_player(self, pid: str):
        self.players.pop(pid, None)

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def color_for_slot(self) -> str:
        used = {p.color for p in self.players.values()}
        for c in COLORS:
            if c not in used:
                return c
        return random.choice(COLORS)

    # ── Broadcast helpers ─────────────────────────────────────────────────────
    async def broadcast(self, msg: dict):
        dead = []
        for p in self.players.values():
            if p.ws and not p.is_ai:
                try:
                    await p.ws.send_json(msg)
                except Exception:
                    dead.append(p.id)
        for pid in dead:
            self.remove_player(pid)

    async def send_state(self):
        await self.broadcast({
            "type":    "game_state",
            "players": [p.to_dict() for p in self.players.values()],
            "state":   self.state,
            "countdown": self.countdown,
            "winner":  self.winner,
            "platform_size": self.platform_size,
        })

    async def send_chat(self, sender: str, msg: str, system: bool = False):
        entry = {"sender": sender, "msg": msg, "system": system, "ts": time.time()}
        self.chat.append(entry)
        await self.broadcast({"type": "chat", **entry})

    # ── Game loop ─────────────────────────────────────────────────────────────
    async def start_loop(self):
        self._task = asyncio.create_task(self._game_loop())

    async def _game_loop(self):
        """Countdown → playing → physics ticks → end"""
        # Countdown
        self.state = "countdown"
        for i in range(COUNTDOWN_SECS, 0, -1):
            self.countdown = i
            await self.send_state()
            await asyncio.sleep(1)

        # Spawn everyone
        all_players = list(self.players.values())
        for i, p in enumerate(all_players):
            p.spawn(i, len(all_players))

        self.state = "playing"
        self.countdown = 0
        await self.send_chat("", "🥊 FIGHT!", system=True)
        await self.send_state()

        while True:
            now = time.time()
            dt  = now - self._last_tick
            self._last_tick = now

            # AI decisions
            for p in list(self.players.values()):
                if p.is_ai and p.alive:
                    self._ai_tick(p, now)

            # Physics
            self._physics_tick(dt)

            # Check eliminations (square platform bounds)
            half = self.platform_size / 2
            eliminated = [p for p in self.players.values()
                          if p.alive and (p.y < FALL_THRESHOLD or
                             abs(p.x) > half + 2 or abs(p.z) > half + 2)]
            for p in eliminated:
                p.alive = False
                p.deaths += 1
                # Shrink platform
                self.platform_size = max(PLATFORM_MIN, self.platform_size - PLATFORM_SHRINK)
                await self.send_chat("", f"💀 {p.name} fell off! Platform shrinking!", system=True)

            # Check win condition
            alive = self.alive_players()
            if len(alive) <= 1:
                self.state = "ended"
                if alive:
                    self.winner = alive[0].id
                    alive[0].kills += 1
                    winner_name = alive[0].name
                    # Update global leaderboard and account
                    leaderboard[winner_name] = leaderboard.get(winner_name, 0) + 1
                    if winner_name in accounts:
                        accounts[winner_name]["wins"] += 1
                        accounts[winner_name]["games"] += 1
                        save_accounts()
                    await self.send_chat("", f"🏆 {winner_name} wins!", system=True)
                else:
                    self.winner = None
                    await self.send_chat("", "💥 Everyone fell off — draw!", system=True)
                await self.send_state()
                await asyncio.sleep(5)
                # Reset to waiting
                self.state   = "waiting"
                self.winner  = None
                self.platform_size = PLATFORM_SIZE
                for p in self.players.values():
                    p.alive = True
                    p.spawn(0, 1)
                await self.send_state()
                break

            await self.send_state()
            await asyncio.sleep(TICK_RATE)

    def _physics_tick(self, dt: float):
        half = self.platform_size / 2
        for p in self.players.values():
            if not p.alive:
                continue
            if not p.on_ground:
                p.vy += GRAVITY * dt
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.z += p.vz * dt
            # Ground collision — square platform
            if p.y <= GROUND_Y:
                if abs(p.x) < half and abs(p.z) < half:
                    p.y        = GROUND_Y
                    p.vy       = 0.0
                    p.on_ground = True
                else:
                    p.on_ground = False
            else:
                p.on_ground = False
            friction = 0.80 if p.on_ground else 0.97
            p.vx *= friction
            p.vz *= friction

    def _punch(self, attacker: Player, target: Player, power: float = 5.0):
        now = time.time()
        if now - attacker.last_punch < PUNCH_COOLDOWN:
            return
        dx = target.x - attacker.x
        dz = target.z - attacker.z
        dist = math.sqrt(dx**2 + dz**2)
        if dist > PUNCH_RANGE or dist == 0:
            return
        attacker.last_punch = now
        nx, nz = dx / dist, dz / dist
        # Scale force by power (1-10)
        t = max(0, min(10, power)) / 10.0
        force = PUNCH_FORCE_MIN + t * (PUNCH_FORCE_MAX - PUNCH_FORCE_MIN)
        target.vx  += nx * force
        target.vz  += nz * force
        target.vy  += force * 0.3
        target.on_ground = False
        attacker.kills += 1

    # ── AI logic ──────────────────────────────────────────────────────────────
    def _ai_tick(self, ai: Player, now: float):
        if now < ai.ai_next_decision:
            return
        ai.ai_next_decision = now + AI_TICK + random.uniform(0, 0.2)

        alive_enemies = [p for p in self.players.values()
                         if p.alive and p.id != ai.id]
        if not alive_enemies:
            return

        def dist_to(p):
            return math.sqrt((p.x-ai.x)**2 + (p.z-ai.z)**2)
        target = min(alive_enemies, key=dist_to)
        ai.ai_target = target.id

        dx = target.x - ai.x
        dz = target.z - ai.z
        dist = dist_to(target)

        # Stay on square platform — retreat if near edge
        half = self.platform_size / 2
        if abs(ai.x) > half * 0.8 or abs(ai.z) > half * 0.8:
            ai.vx += (-ai.x / max(abs(ai.x), 0.1)) * PLAYER_SPEED * 0.6
            ai.vz += (-ai.z / max(abs(ai.z), 0.1)) * PLAYER_SPEED * 0.6
            return

        if dist < PUNCH_RANGE:
            power = random.uniform(5, 10)
            self._punch(ai, target, power)
        else:
            if dist > 0:
                spd = PLAYER_SPEED * random.uniform(0.6, 1.0)
                ai.vx += (dx / dist) * spd * AI_TICK
                ai.vz += (dz / dist) * spd * AI_TICK
                ai.rot_y = math.atan2(dx, dz)

        if ai.on_ground and random.random() < 0.05:
            ai.vy = JUMP_VEL
            ai.on_ground = False


# ─── HTTP Endpoints ────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/lobbies")
async def list_lobbies():
    return [
        {
            "id":      lid,
            "host":    lb.host_name,
            "players": len(lb.players),
            "max":     LOBBY_MAX,
            "state":   lb.state,
        }
        for lid, lb in lobbies.items()
    ]

@app.get("/leaderboard")
async def get_leaderboard():
    sorted_lb = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    return [{"name": k, "wins": v} for k, v in sorted_lb[:20]]

from fastapi import Body

@app.post("/register")
async def register(data: dict = Body(...)):
    username = str(data.get("username", "")).strip()[:16]
    password = str(data.get("password", "")).strip()
    if not username or not password:
        return {"ok": False, "msg": "Username and password required"}
    if len(password) < 4:
        return {"ok": False, "msg": "Password must be at least 4 characters"}
    if username in accounts:
        return {"ok": False, "msg": "Username already taken!"}
    accounts[username] = {"password": password, "wins": 0, "games": 0}
    leaderboard[username] = 0
    save_accounts()
    return {"ok": True, "msg": "Account created!", "username": username}

@app.post("/login")
async def login(data: dict = Body(...)):
    username = str(data.get("username", "")).strip()[:16]
    password = str(data.get("password", "")).strip()
    if username not in accounts:
        return {"ok": False, "msg": "Account not found"}
    if accounts[username]["password"] != password:
        return {"ok": False, "msg": "Wrong password!"}
    wins  = accounts[username]["wins"]
    games = accounts[username]["games"]
    return {"ok": True, "username": username, "wins": wins, "games": games}


# ─── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws/{lobby_id}/{player_name}")
async def ws_endpoint(ws: WebSocket, lobby_id: str, player_name: str):
    await ws.accept()

    # Create or join lobby
    if lobby_id not in lobbies:
        lobbies[lobby_id] = Lobby(lobby_id, player_name)

    lobby = lobbies[lobby_id]

    if len(lobby.players) >= LOBBY_MAX:
        await ws.send_json({"type": "error", "msg": "Lobby full"})
        await ws.close()
        return

    pid   = str(uuid.uuid4())[:8]
    color = lobby.color_for_slot()
    player = Player(pid, player_name[:16], color)
    player.ws = ws
    lobby.add_player(player)

    await ws.send_json({"type": "joined", "your_id": pid, "lobby_id": lobby_id})
    await lobby.send_chat("", f"👋 {player_name} joined!", system=True)
    await lobby.send_state()

    # Add AI bots if lobby just became ready and no game running
    if (lobby.state == "waiting"
            and len(lobby.alive_players()) >= LOBBY_START_MIN
            and lobby._task is None):
        await _fill_with_bots(lobby)

    try:
        while True:
            data = await ws.receive_json()
            await _handle_message(lobby, player, data)
    except (WebSocketDisconnect, Exception):
        lobby.remove_player(pid)
        await lobby.send_chat("", f"👋 {player_name} left.", system=True)
        if len(lobby.players) == 0:
            lobbies.pop(lobby_id, None)
        else:
            await lobby.send_state()


async def _fill_with_bots(lobby: Lobby):
    bot_names = ["BotZap","BotKing","BotBash","BotSlam","BotPunch","BotKO","BotRex","BotNova"]
    current   = len(lobby.players)
    target    = min(LOBBY_MAX, current + 8)
    for i in range(current, target):
        name  = bot_names[i % len(bot_names)] + str(random.randint(1, 99))
        pid   = "bot_" + str(uuid.uuid4())[:6]
        color = lobby.color_for_slot()
        bot   = Player(pid, name, color, is_ai=True)
        lobby.add_player(bot)
    await lobby.send_chat("", f"🤖 Bots added! Starting soon…", system=True)
    await lobby.start_loop()


async def _handle_message(lobby: Lobby, player: Player, data: dict):
    kind = data.get("type")

    if kind == "input" and lobby.state == "playing" and player.alive:
        inp = data.get("input", {})
        dx = float(inp.get("dx", 0))
        dz = float(inp.get("dz", 0))

        length = math.sqrt(dx**2 + dz**2)
        if length > 0:
            dx /= length
            dz /= length
            player.vx += dx * PLAYER_SPEED * TICK_RATE * 12
            player.vz += dz * PLAYER_SPEED * TICK_RATE * 12
            player.rot_y = math.atan2(dx, dz)

        if inp.get("jump") and player.on_ground:
            player.vy = JUMP_VEL
            player.on_ground = False

        if inp.get("punch"):
            power = float(inp.get("power", 5.0))
            best, best_dist = None, float("inf")
            for other in lobby.alive_players():
                if other.id == player.id:
                    continue
                d = math.sqrt((other.x-player.x)**2 + (other.z-player.z)**2)
                if d < best_dist:
                    best, best_dist = other, d
            if best:
                lobby._punch(player, best, power)

    elif kind == "chat":
        msg = str(data.get("msg", ""))[:200].strip()
        if msg:
            await lobby.send_chat(player.name, msg)

    elif kind == "start" and lobby.state == "waiting":
        if len(lobby.players) >= LOBBY_START_MIN and lobby._task is None:
            await _fill_with_bots(lobby)

    elif kind == "add_bot" and lobby.state == "waiting":
        if len(lobby.players) < LOBBY_MAX:
            await _fill_with_bots(lobby)


# ─── Static files (after routes) ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
