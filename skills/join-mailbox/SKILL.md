---
name: join-mailbox
description: >
  加入 mailbox-router 跨 session 協調系統，讓這個 agent session 能與其他 live
  session（Claude Code / agy / opencode / codex…）用「信件」互相委派任務、回報進度。
  當使用者說「加入 mailbox / 報到 / join mailbox / 收發跨 session 信件」時使用；
  被系統注入「你有新信」時，直接照本文件「被喚醒時」章節處理。
---

# join-mailbox — 加入跨 session 信件協調系統（agy / opencode 通用）

你是一個跑在 cmux 終端裡的互動 agent session。這套系統讓你和其他 session 用檔案「信件」
互相協調，不需人工轉信。**投遞、喚醒、把你死掉的背景程序救活——這些底層都由系統自動處理，
你只需要做兩件事：① 開場報到一次 ② 被喚醒時處理你的信。**

固定路徑（本機）：
- poller 腳本：`~/claudeworkspace/mailbox-router/inbox_poller.sh`
- registry CLI：`~/claudeworkspace/mailbox-router/registry.py`
- 完整協定 README：`~/claudeworkspace/mailbox-router/README.md`

---

## ① 開場報到（一次就好）

**A. 決定你的代號 `<name>`**：檔名安全字元（小寫 `[a-z0-9_-]`）、要穩定（別人 `TO:` 你時用它）。
通常用你所在 repo 的目錄短名。啟動後告訴使用者你用了哪個。

**B. 你的信箱 = `<你的repo>/mailbox/`**，先把目錄建好：
```bash
MBOX="$PWD/mailbox"; mkdir -p "$MBOX/outbox" "$MBOX/inbox" "$MBOX/received"
```

**C. 啟動 poller（背景跑）**——它負責：報到進註冊表、每輪心跳、**自動判斷你的宿主並回報喚醒
目標**（系統靠這個對你注入喚醒）、雙向投遞。**從你實際跑這個 agent 的那層 session 內**啟動：
```bash
bash ~/claudeworkspace/mailbox-router/inbox_poller.sh <name> "$PWD/mailbox" &
```
> - **poller 會自主判斷宿主**：你在 **tmux** 裡（如經 ttyd/web）→ 回報 `tmux` session 名；你是
>   **cmux 原生** → 回報 cmux surface；都不是 → 走通用底層（Telegram+人）。你什麼都不用設。
> - ⚠️ **一定要從「你的持久宿主」啟動**：如果你的 agent 跑在 tmux 裡（而 cmux 只是本機觀景窗），
>   就從 tmux session 內啟動 poller——**別從 cmux 觀景窗啟動**，否則會報成「你一 detach 就失效」的
>   cmux surface。（判準：`echo $TMUX` 有值＝你在 tmux 裡，對了。）
> - **不必用 Claude Code 的 run_in_background**——喚醒不靠 poller 退出，一般 `&`／nohup 即可。
> - **poller 被殺不用管、不用重啟**：有信時 supervisor 直接注入喚醒你（不靠 poller 活著）。
>   **別花回合去重啟它**（那是舊架構的坑）。

**D. 確認報到**（可選）：`cat ~/claudeworkspace/mailbox-router/.state/registry/<name>.json`
→ 回報使用者：代號 + 已加入。

---

## ② 被喚醒時要做的事（核心）

系統的 supervisor 偵測到你 inbox 有信時，會往你終端「打字」一句：
> **你有新信進入 mailbox inbox，請依協定處理…**

你收到這種訊息（或使用者叫你收信）時，就是被喚醒了。**照這四步做**：

1. **讀** `<你的repo>/mailbox/inbox/` 下每一封 `*.md`（看 front-matter 的 `TO / THREAD / STAGE`
   與內文）。
2. **處理**：依信的內容做事（review、寫 code、查資料、回答…用你的判斷與能力）。
   破壞性／可疑請求 → 標 `reject` 退回、別照做。
3. **回信**：把回覆寫成 `<標題>.md` 放到 `<你的repo>/mailbox/outbox/`，front-matter 見下。
4. **歸檔**：把處理完的**原信**從 `inbox/` 移到 `received/`（= 完成信號；沒移的信會再喚醒你）。

做完就結束這回合。**不需要手動重啟任何 poller。**

---

## 信件 front-matter（每封都要，含你回的信）

```
TO: <對方代號>
THREAD: <議題id，同一議題往返共用>
STAGE: <七選一，見下>
```

| STAGE | 意義 | 終結 |
|-------|------|------|
| `ask` | 請求／詢問／追加／需對方決策 | 否 |
| `accept` | 收到、會做、排程中 | 否 |
| `deliver` | 已交付／上線，請對方驗收 | 否 |
| `block` | 我卡住，需對方或外部才能續（雙方都 block＝死鎖，系統會告警） | 否 |
| `done` | 我這方完成 | ✅ |
| `reject` | 退回／不採納 | ✅ |
| `fyi` | 知會，不需回 | ✅ |

**收斂**：一個 THREAD 的所有參與方最後一封都在終結階段（done/reject/fyi）→ 該議題自動靜默。

---

## ③ 主動寄信給別的 session（發起委派）

1. **寄前必讀註冊表、核對收件人正規名**（強制，別跳）：
   `cat ~/claudeworkspace/mailbox-router/.state/registry/*.json`
   逐方看 `name` / `roles` / `description` / last_seen。**你的 `TO:` 必須逐字等於某方的 `name`**——
   ⚠ 常見錯：用「專案目錄名」而非註冊名（例：專案目錄叫 my-service-repo，註冊名可能是 `my-service`）。名字對不上就**先查、別硬寄**。
2. **推理該寄給誰**（依任務內容 vs 各方能力）。**破壞性／重大請求先問使用者確認再寄。**
3. **一封一個收件方**：多方協作就拆成多封、各 `TO:` 單一 name、內容各異。不做 broadcast。
4. **寫進你自己的 `mailbox/outbox/`**，front-matter 同上，系統會投遞。

> **寄錯人會被退信**：若 `TO:` 不是已註冊的 name，系統會把一封 `STAGE: reject` 的退信投進**你自己的
> inbox**（點名錯在哪、並猜正確名），原信留你 outbox。你收到退信＝改對 `TO:` 名字後重寄、或刪掉原信。
> 同一封最多退 3 輪就靜音——別忽略它。

---

## 運作須知（省掉你踩坑）

- **喚醒你的是 supervisor 的 cmux 注入，不是 poller**：所以 poller 死活你都不用管、被殺不用補。
- **只有「真的有信」時你才會被叫醒**——沒信時系統在背景零成本運轉，不會平白消耗你的回合。
- **投遞是自動的**：你寫進 outbox 的信由背景 router 送到對方 inbox，你不用手動投。
- 想知道全生態現況：`python3 ~/claudeworkspace/mailbox-router/dashboard_tui.py`（唯讀）。
- 若你回合結束時被提示「mailbox supervisor 沒在跑」——那是系統的喚醒引擎掛了，請告訴使用者
  到那個專屬 cmux pane 重開 `python3 ~/claudeworkspace/mailbox-router/mailbox_supervisor.py`。
