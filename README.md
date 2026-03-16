# 🥊 KNOCKOUT! — 3D Browser Multiplayer Game

A Roblox-style knockout game playable in the browser.
Last player standing on the platform wins!

---

## 📁 Project Structure

```
knockout-game/
├── server.py          ← Python backend (FastAPI + WebSockets)
├── requirements.txt   ← Python dependencies
├── README.md
└── static/
    └── index.html     ← Full 3D game (Three.js)
```

---

## 🚀 Setup & Run (VS Code)

### 1. Install Python dependencies
Open a terminal in VS Code (`Ctrl + ~`) and run:

```bash
pip install -r requirements.txt
```

### 2. Start the server
```bash
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 3. Open the game
Open your browser and go to:
```
http://localhost:8000
```

---

## 🎮 How to Play

| Action | Key |
|--------|-----|
| Move   | WASD or Arrow Keys |
| Jump   | Space |
| Punch  | F or Click |

- **Knock** other players off the platform!
- **Fall off** = eliminated
- **Last one standing** wins 🏆

---

## 👥 Multiplayer & Lobbies

1. Enter your name and a lobby name
2. Share the **lobby name** with friends — they type the same name to join
3. AI bots fill empty slots automatically
4. Up to **8 players** per lobby (mix of real + AI)

---

## 🤖 AI Bots

- Bots automatically join when a real player creates/joins a lobby
- They chase the nearest enemy, punch them off, and avoid the edge
- Names start with "Bot" (e.g. BotKing42)

---

## 🏆 Leaderboard

- Global win tracking across all games
- Click **View Leaderboard** on the menu, or it auto-updates after each game

---

## ⚙️ Configuration (server.py)

You can tweak these constants at the top of `server.py`:

```python
PLATFORM_RADIUS = 12.0    # Size of the arena
PUNCH_FORCE     = 18.0    # How hard punches send players flying
PUNCH_RANGE     = 2.5     # How close you need to be to punch
LOBBY_MAX       = 8       # Max players per lobby
PLAYER_SPEED    = 6.0     # Movement speed
```

---

## 🌐 Play Over LAN (with friends on same WiFi)

Share your local IP instead of localhost:

```bash
# Find your IP
ipconfig    # Windows
ifconfig    # Mac/Linux
```

Friends go to: `http://YOUR_IP:8000`

---

## 🛠 Troubleshooting

**"Server offline" or can't connect:**
- Make sure `uvicorn` is running in the terminal
- Check you're on `http://localhost:8000` (not https)

**Game is slow:**
- Reduce `TICK_RATE` in server.py (e.g. `0.08`)
- Lower the pixel ratio in `index.html` (`Math.min(devicePixelRatio, 1)`)

**Port already in use:**
- Change port: `uvicorn server:app --port 8001`
- Update `WS_PORT = 8001` in `static/index.html`
