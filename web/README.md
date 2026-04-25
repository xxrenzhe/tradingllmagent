# Trading LLM Agent Web Console

Local React/Vite console for the FastAPI research server.

## Run locally

```bash
python3 -m pip install -e ".[api]"
TLM_TASK_DB=experiments/tasks.sqlite3 uvicorn tlm.api:app --reload
cd web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Leave the API base field empty when using the Vite proxy, or set it to `http://127.0.0.1:8000`.

## Scope

- Creates local data download, bar build, tick backtest, and single- or multi-spec research tasks.
- Streams task status and logs from Server-Sent Events.
- Shows data quality, leaderboard, rejected strategies, experiment details, and audit logs.
- Does not expose live brokerage actions or NinjaTrader live execution controls.
