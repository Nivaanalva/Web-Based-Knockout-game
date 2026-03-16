"""
Knockout Game Server — Full rewrite
FastAPI + WebSockets
Run: uvicorn server:app --reload --host 0.0.0.0 --port 8000
"""
import os
import asyncio
import json
import math
import random
import time
import uuid
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Knockout Game Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Constants ────────────────────────────────────────────────────────────────
PLATFORM_SIZE     = 24.0
PLATFORM_MIN      = 10.0
PLATFORM_SHRINK   = 2.0
FALL_THRESHOLD    = -6.0
TICK_RATE         = 0.05
PUNCH_RANGE       = 7.0       # long range aim punch
PUNCH_CONE        = 0.7       # how wide the punch cone is (radians)
PUNCH_FORCE_MIN   = 10.0
PUNCH_FORCE_MAX   = 32.0
PUNCH_COOLDOWN    = 1.0
GRAVITY           = -20.0
GROUND_Y          = 0.0
PLAYER_SPEED      = 3.5
JUMP_VEL          = 9.0
LOBBY_MAX         = 9
COUNTDOWN_SECS    = 5
AI_TICK           = 0.25
ROUND_END_WAIT    = 5         # seconds before next round starts

COLORS = ["#FF6B6B","#FFD93D","#6BCB77","#4D96FF","#C77DFF","#FF9A3C","#00C9A7","#FF61A6"]
BOT_NAMES = ["BotZap","BotKing","BotBash","BotSlam","BotPunch","BotKO","BotRex","BotNova"]

# ─── Global State ─────────────────────────────────────────────────────────────
lobbies:     Dict[str, "Lobby"] = {}
leaderboard: Dict[str, int]     = {}
accounts:    Dict[str, dict]    = {}

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"DB connect error: {e}")
        return None

def init_db():
    conn = get_db()
    if not conn:
        print("No DATABASE_URL — accounts won't persist on Render")
        return
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
            username TEXT PRIMARY KEY, password TEXT NOT NULL,
            wins INTEGER DEFAULT 0, games INTEGER DEFAULT 0)""")
        conn.commit()
        cur.execute("SELECT username, password, wins, games FROM accounts")
        for row in cur.fetchall():
            accounts[row[0]] = {"password":row[1],"wins":row[2],"games":row[3]}
            leaderboard[row[0]] = row[2]
        print(f"Loaded {len(accounts)} accounts from PostgreSQL")
        cur.close(); conn.close()
    except Exception as e:
        print(f"DB init error: {e}")

def save_account_db(username, data):
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO accounts (username,password,wins,games) VALUES (%s,%s,%s,%s)
            ON CONFLICT (username) DO UPDATE SET password=EXCLUDED.password,
            wins=EXCLUDED.wins, games=EXCLUDED.games""",
            (username, data["password"], data["wins"], data["games"]))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"DB save error: {e}")

ACCOUNTS_FILE = "accounts.json"

def load_accounts():
    if DATABASE_URL:
        init_db(); return
    try:
        with open(ACCOUNTS_FILE,"r") as f:
            data = json.load(f)
            accounts.update(data)
            for u,d in data.items():
                leaderboard[u] = d.get("wins",0)
    except FileNotFoundError:
        pass

def save_accounts():
    if DATABASE_URL: return
    try:
        with open(ACCOUNTS_FILE,"w") as f: json.dump(accounts, f)
    except: pass

load_accounts()

# ─── Player ───────────────────────────────────────────────────────────────────
class Player:
    def __init__(self, pid, name, color, is_ai=False):
        self.id = pid; self.name = name; self.color = color; self.is_ai = is_ai
        self.ws = None
        self.x = self.y = self.z = 0.0
        self.vx = self.vy = self.vz = 0.0
        self.rot_y = 0.0
        self.alive = True; self.on_ground = False
        self.last_punch = 0.0
        self.kills = 0; self.deaths = 0
        self.ai_target = None; self.ai_next_decision = 0.0

    def to_dict(self):
        return {"id":self.id,"name":self.name,"color":self.color,"is_ai":self.is_ai,
                "x":self.x,"y":self.y,"z":self.z,"vx":self.vx,"vy":self.vy,"vz":self.vz,
                "rot_y":self.rot_y,"alive":self.alive,"kills":self.kills,"deaths":self.deaths}

    def spawn(self, index, total):
        angle = (2 * math.pi * index) / max(total, 1)
        r = (PLATFORM_SIZE / 2) * 0.5
        self.x = r * math.cos(angle); self.y = GROUND_Y; self.z = r * math.sin(angle)
        self.vx = self.vy = self.vz = 0.0
        self.alive = True; self.on_ground = True
        self.rot_y = angle + math.pi  # face center

# ─── Lobby ────────────────────────────────────────────────────────────────────
class Lobby:
    def __init__(self, lobby_id, host_name, bot_mode=False):
        self.id = lobby_id; self.host_name = host_name
        self.bot_mode = bot_mode  # True = solo vs bots, False = multiplayer
        self.players: Dict[str, Player] = {}
        self.chat: List[dict] = []
        self.state = "waiting"
        self.countdown = COUNTDOWN_SECS
        self.winner = None
        self.platform_size = PLATFORM_SIZE
        self._task = None
        self._last_tick = time.time()
        self._running = True

    def add_player(self, p): self.players[p.id] = p
    def remove_player(self, pid): self.players.pop(pid, None)
    def alive_players(self): return [p for p in self.players.values() if p.alive]

    def color_for_slot(self):
        used = {p.color for p in self.players.values()}
        for c in COLORS:
            if c not in used: return c
        return random.choice(COLORS)

    async def broadcast(self, msg):
        dead = []
        for p in self.players.values():
            if p.ws and not p.is_ai:
                try: await p.ws.send_json(msg)
                except: dead.append(p.id)
        for pid in dead: self.remove_player(pid)

    async def send_state(self):
        await self.broadcast({"type":"game_state",
            "players":[p.to_dict() for p in self.players.values()],
            "state":self.state,"countdown":self.countdown,
            "winner":self.winner,"platform_size":self.platform_size,
            "bot_mode":self.bot_mode})

    async def send_chat(self, sender, msg, system=False):
        entry = {"sender":sender,"msg":msg,"system":system,"ts":time.time()}
        self.chat.append(entry)
        await self.broadcast({"type":"chat",**entry})

    async def start_loop(self):
        self._task = asyncio.create_task(self._game_loop())

    async def _game_loop(self):
        """Runs forever — restarts a new round after each one ends."""
        while self._running:
            # Add bots if bot mode
            if self.bot_mode:
                self._add_bots()

            # Wait if no real players
            while not any(not p.is_ai for p in self.players.values()):
                await asyncio.sleep(1)
                if not self._running: return

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
            self.platform_size = PLATFORM_SIZE
            await self.send_chat("", "🐧 FIGHT!", system=True)
            await self.send_state()
            self._last_tick = time.time()

            # Game tick loop
            while self._running:
                now = time.time()
                dt = min(now - self._last_tick, 0.1)
                self._last_tick = now

                for p in list(self.players.values()):
                    if p.is_ai and p.alive: self._ai_tick(p, now)

                self._physics_tick(dt)

                # Eliminations
                half = self.platform_size / 2
                for p in list(self.players.values()):
                    if p.alive and (p.y < FALL_THRESHOLD or abs(p.x) > half+3 or abs(p.z) > half+3):
                        p.alive = False; p.deaths += 1
                        self.platform_size = max(PLATFORM_MIN, self.platform_size - PLATFORM_SHRINK)
                        await self.send_chat("", f"💀 {p.name} fell off!", system=True)

                alive = self.alive_players()
                if len(alive) <= 1:
                    self.state = "ended"
                    if alive:
                        self.winner = alive[0].id
                        winner_name = alive[0].name
                        leaderboard[winner_name] = leaderboard.get(winner_name, 0) + 1
                        if winner_name in accounts:
                            accounts[winner_name]["wins"] += 1
                            accounts[winner_name]["games"] += 1
                            save_accounts()
                            if DATABASE_URL: save_account_db(winner_name, accounts[winner_name])
                        await self.send_chat("", f"🏆 {winner_name} wins!", system=True)
                    else:
                        self.winner = None
                        await self.send_chat("", "💥 Draw!", system=True)
                    await self.send_state()
                    await asyncio.sleep(ROUND_END_WAIT)
                    # Reset all players alive for next round
                    for p in list(self.players.values()):
                        p.alive = True; p.kills = 0
                    self.winner = None
                    break  # break inner loop → restart outer loop (new round)

                await self.send_state()
                await asyncio.sleep(TICK_RATE)

    def _add_bots(self):
        """Fill lobby with 8 bots."""
        existing_bots = [p for p in self.players.values() if p.is_ai]
        for b in existing_bots: self.remove_player(b.id)
        for i in range(8):
            name = BOT_NAMES[i % len(BOT_NAMES)] + str(random.randint(1, 99))
            pid  = "bot_" + str(uuid.uuid4())[:6]
            color = self.color_for_slot()
            bot = Player(pid, name, color, is_ai=True)
            self.add_player(bot)

    def _physics_tick(self, dt):
        half = self.platform_size / 2
        for p in self.players.values():
            if not p.alive: continue
            if not p.on_ground: p.vy += GRAVITY * dt
            p.x += p.vx * dt; p.y += p.vy * dt; p.z += p.vz * dt
            if p.y <= GROUND_Y:
                if abs(p.x) < half and abs(p.z) < half:
                    p.y = GROUND_Y; p.vy = 0.0; p.on_ground = True
                else:
                    p.on_ground = False
            else:
                p.on_ground = False
            friction = 0.78 if p.on_ground else 0.97
            p.vx *= friction; p.vz *= friction

    def _punch(self, attacker, power=5.0):
        """Directional punch — hits anyone in a cone in front of attacker."""
        now = time.time()
        if now - attacker.last_punch < PUNCH_COOLDOWN: return
        # Direction attacker is facing
        face_x = math.sin(attacker.rot_y)
        face_z = math.cos(attacker.rot_y)
        hit_any = False
        for target in self.alive_players():
            if target.id == attacker.id: continue
            dx = target.x - attacker.x
            dz = target.z - attacker.z
            dist = math.sqrt(dx**2 + dz**2)
            if dist == 0 or dist > PUNCH_RANGE: continue
            # Check if target is in punch cone
            dot = (dx/dist)*face_x + (dz/dist)*face_z
            if dot < math.cos(PUNCH_CONE): continue
            # Hit!
            hit_any = True
            t = max(0, min(10, power)) / 10.0
            force = PUNCH_FORCE_MIN + t * (PUNCH_FORCE_MAX - PUNCH_FORCE_MIN)
            # Knock away from attacker
            target.vx += (dx/dist) * force
            target.vz += (dz/dist) * force
            target.vy += force * 0.35
            target.on_ground = False
        if hit_any:
            attacker.last_punch = now
            attacker.kills += 1

    def _ai_tick(self, ai, now):
        if now < ai.ai_next_decision: return
        ai.ai_next_decision = now + AI_TICK + random.uniform(0, 0.15)

        enemies = [p for p in self.players.values() if p.alive and p.id != ai.id]
        if not enemies: return

        def dist_to(p): return math.sqrt((p.x-ai.x)**2 + (p.z-ai.z)**2)
        target = min(enemies, key=dist_to)
        dx = target.x - ai.x
        dz = target.z - ai.z
        dist = dist_to(target)

        # Avoid edge
        half = self.platform_size / 2
        if abs(ai.x) > half*0.82 or abs(ai.z) > half*0.82:
            ai.vx += (-ai.x/max(abs(ai.x),0.1)) * PLAYER_SPEED * 0.7
            ai.vz += (-ai.z/max(abs(ai.z),0.1)) * PLAYER_SPEED * 0.7
            return

        # Face target
        ai.rot_y = math.atan2(dx, dz)

        if dist < PUNCH_RANGE * 0.85:
            # Try to punch
            power = random.uniform(4, 10)
            self._punch(ai, power)
        else:
            # Move toward target
            spd = PLAYER_SPEED * random.uniform(0.7, 1.0)
            ai.vx += (dx/dist) * spd * AI_TICK
            ai.vz += (dz/dist) * spd * AI_TICK

        if ai.on_ground and random.random() < 0.04:
            ai.vy = JUMP_VEL; ai.on_ground = False


# ─── HTTP Endpoints ────────────────────────────────────────────────────────────
@app.get("/")
async def root(): return FileResponse("static/index.html")

@app.get("/lobbies")
async def list_lobbies():
    return [{"id":lid,"host":lb.host_name,"players":len(lb.players),
             "max":LOBBY_MAX,"state":lb.state,"bot_mode":lb.bot_mode}
            for lid,lb in lobbies.items()]

@app.get("/leaderboard")
async def get_leaderboard():
    s = sorted(leaderboard.items(), key=lambda x:x[1], reverse=True)
    return [{"name":k,"wins":v} for k,v in s[:20]]

@app.post("/register")
async def register(data: dict = Body(...)):
    username = str(data.get("username","")).strip()[:16]
    password = str(data.get("password","")).strip()
    if not username or not password: return {"ok":False,"msg":"Fill in all fields!"}
    if len(password) < 4: return {"ok":False,"msg":"Password must be 4+ characters"}
    if username in accounts: return {"ok":False,"msg":"Username already taken!"}
    accounts[username] = {"password":password,"wins":0,"games":0}
    leaderboard[username] = 0
    save_accounts()
    if DATABASE_URL: save_account_db(username, accounts[username])
    return {"ok":True,"username":username}

@app.post("/login")
async def login(data: dict = Body(...)):
    username = str(data.get("username","")).strip()[:16]
    password = str(data.get("password","")).strip()
    if username not in accounts: return {"ok":False,"msg":"Account not found!"}
    if accounts[username]["password"] != password: return {"ok":False,"msg":"Wrong password!"}
    d = accounts[username]
    return {"ok":True,"username":username,"wins":d["wins"],"games":d["games"]}


# ─── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws/{lobby_id}/{player_name}/{bot_mode}")
async def ws_endpoint(ws: WebSocket, lobby_id: str, player_name: str, bot_mode: str):
    await ws.accept()
    is_bot_mode = bot_mode == "bots"

    if lobby_id not in lobbies:
        lobbies[lobby_id] = Lobby(lobby_id, player_name, bot_mode=is_bot_mode)

    lobby = lobbies[lobby_id]

    if len(lobby.players) >= LOBBY_MAX:
        await ws.send_json({"type":"error","msg":"Lobby full"})
        await ws.close(); return

    pid = str(uuid.uuid4())[:8]
    color = lobby.color_for_slot()
    player = Player(pid, player_name[:16], color)
    player.ws = ws
    lobby.add_player(player)

    await ws.send_json({"type":"joined","your_id":pid,"lobby_id":lobby_id})
    await lobby.send_chat("", f"🐧 {player_name} joined!", system=True)
    await lobby.send_state()

    # Start game loop if not already running
    if lobby._task is None or lobby._task.done():
        await lobby.start_loop()

    try:
        while True:
            data = await ws.receive_json()
            await _handle_message(lobby, player, data)
    except (WebSocketDisconnect, Exception):
        lobby.remove_player(pid)
        await lobby.send_chat("", f"👋 {player_name} left.", system=True)
        if len([p for p in lobby.players.values() if not p.is_ai]) == 0:
            lobby._running = False
            lobbies.pop(lobby_id, None)
        else:
            await lobby.send_state()


async def _handle_message(lobby, player, data):
    kind = data.get("type")
    if kind == "input" and lobby.state == "playing" and player.alive:
        inp = data.get("input", {})
        dx = float(inp.get("dx", 0))
        dz = float(inp.get("dz", 0))
        length = math.sqrt(dx**2 + dz**2)
        if length > 0:
            dx /= length; dz /= length
            player.vx += dx * PLAYER_SPEED * TICK_RATE * 10
            player.vz += dz * PLAYER_SPEED * TICK_RATE * 10

        # Update facing direction from mouse aim if provided
        aim_angle = inp.get("aim_angle")
        if aim_angle is not None:
            player.rot_y = float(aim_angle)
        elif length > 0:
            player.rot_y = math.atan2(dx, dz)

        if inp.get("jump") and player.on_ground:
            player.vy = JUMP_VEL; player.on_ground = False

        if inp.get("punch"):
            power = float(inp.get("power", 5.0))
            lobby._punch(player, power)

    elif kind == "chat":
        msg = str(data.get("msg",""))[:200].strip()
        if msg: await lobby.send_chat(player.name, msg)


# ─── Static files ─────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
