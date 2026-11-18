# Skill Explorer

自動追蹤各大廠官方與 GitHub 高星的 Agent Skills（`SKILL.md`），每天排程抓取，並在網頁上說明每個 skill「厲害在哪、可以應用在哪」。

## 快速開始

```bash
cd Skill_explorer
cp .env.example .env        # 填 OPENAI_API_KEY、GITHUB_TOKEN（都選填）
docker compose up -d --build
open http://127.0.0.1:5000
```

- 改 `.env` 後：`docker compose up -d`（會重建容器套用新變數）
- 改 `config/sources.yaml`：不用重啟，下次抓取自動生效
- 看 log：`docker compose logs -f`
- 資料存在 `./data/skills.db`，重建容器不會遺失
- 不用 Docker：`pip install -r requirements.txt && ./start.sh`

第一次啟動時資料庫是空的，會自動在背景抓一次；之後依 `config/sources.yaml` 的 `schedule.cron`（預設每天 03:00）自動更新。網頁右上角「立即更新」可手動觸發。

## 來源（`config/sources.yaml`）

| 類型 | 說明 |
|---|---|
| `repo` | 指定 repo，掃描裡面所有 `SKILL.md`（官方來源都用這種） |
| `search` | GitHub 搜尋（例如 `topic:agent-skills`），取星星數前 N 個 repo 再掃描 |

已內建：Anthropic、OpenAI、Google（google/skills、gemini-skills）、Microsoft/Azure、AWS（2 個）、Hugging Face、Vercel、Cloudflare、Supabase，以及社群高星的 obra/superpowers、addyosmani/agent-skills 和兩個 topic 搜尋。
xAI/Grok 目前沒有官方 skill repo，出現後在設定檔加一行即可。改設定不用重啟。

## 「厲害在哪 / 應用場景」怎麼產生

- **OpenAI（預設）**：設定 `OPENAI_API_KEY`，模型由 `OPENAI_MODEL` 指定（預設 `gpt-6-luna`），用 JSON Schema structured output 產生繁中分析。可用 `OPENAI_BASE_URL` 指向 Azure OpenAI 或相容服務。
- **Anthropic**：`LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`。
- 只有內容有變動的 skill 才會重新分析，每次上限 `analysis.max_per_run` 個。
- **沒有 key**：用內建規則，依官方與否、星星數、附帶腳本 / 參考文件數量、關鍵字分類產生分析。

## 每張卡的中文說明

每次抓取後，所有新增或內容有變的 skill 會用 LLM 產生一句 25 字內的中文用途說明（每 25 個打包一次請求，很省）。
點開卡片才看得到的「厲害在哪 / 應用場景」是逐一深入分析，每次上限 `max_per_run` 個。

## 為你推薦

「全部」預設依「為你推薦」排序：星星數 + 是否官方 + `config/sources.yaml` 的 `interests` 關鍵字 + 你按 👍/👎 的紀錄（相似的 skill 會跟著升降）。

## 每日 email 日報

每次排程跑完會產生日報（存在 `data/digests/`），在 `.env` 設定 SMTP 就會寄出：
- 信件內文：今天新發現的 skill + 為你推薦 Top 150（連結 + 中文說明）
- 附件 `all-skills.html`：**全部** skill 依推薦排序（Gmail 超過 102KB 會截斷內文，所以完整清單放附件）
- 網頁上的「寄日報」按鈕可立即寄一份測試

Gmail 設定：`SMTP_HOST=smtp.gmail.com`、`SMTP_PORT=587`、`SMTP_USER=你的 Gmail`、`SMTP_PASSWORD=應用程式密碼`、`MAIL_TO=收件人`。

## 資料保存

- 全部資料存在 SQLite `data/skills.db`（主機上的資料夾，容器刪除或重建都不會遺失）
- 每次排程跑完自動備份到 `data/backups/skills-YYYYMMDD.db`，保留最近 14 份
- 還原：停掉容器，把備份檔複製回 `data/skills.db` 再啟動

## 不開網頁也想排程？

伺服器本身內建排程。若想讓抓取獨立於網頁執行，可用 crontab：

```cron
0 3 * * * cd /Users/william/Documents/project/Skill/Skill_explorer && /opt/anaconda3/bin/python3 -m backend.run_once >> data/cron.log 2>&1
```

## 結構

```
backend/fetcher.py   GitHub 爬蟲（ETag 快取；內容走 raw.githubusercontent.com，不吃 API 額度）
backend/analyzer.py  LLM 中文摘要與深入分析（OpenAI / Anthropic / 規則）
backend/ranking.py   為你推薦排序
backend/digest.py    每日 email 日報
backend/app.py       FastAPI API + APScheduler + 前端
frontend/index.html  單頁介面（搜尋、廠商/分類篩選、詳細頁含原始 SKILL.md）
config/sources.yaml  來源與排程設定
data/skills.db       SQLite
```

## API

`GET /api/skills`（已排序）· `GET /api/skill?id=` · `POST /api/feedback` · `GET /api/status` · `GET /api/runs` · `POST /api/refresh` · `POST /api/digest/send` · `GET /api/digest/latest[?all=1]`
