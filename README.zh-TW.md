[English](./README.md) | [繁體中文](./README.zh-TW.md)

# 結構型商品評價器（Structured Derivative Pricer）— Claude 技能

這是一個為專業機構投資人設計的衍生性金融商品 [skill](https://docs.claude.com/en/docs/claude-code/skills)，
可以替 vanilla、exotic 衍生性商品或結構型商品建立評價模型。在安裝此 skill 後，使用者只需描述商品條件，
AI 即可自動選擇適配的模型元件，如 model、curve、calibration、payoff、early termination 等，
並以蒙地卡羅模擬完成評價（在無封閉解的情況下）。

這個 skill 不透過產生程式碼的方式建立評價模型，而是透過 factory design pattern，將多數常用元件
（如 curve、model、calibration、schedule、market convention、Monte Carlo、Longstaff-Schwartz 等）預先建立好，
透過組裝方式建立出完整評價流程。每個評價模型最終以一個 YAML 檔表達，優點是完全避開
「產生程式碼 → 執行 → 除錯」的循環，以確保穩定產出結果。

---

## 安裝

### 需求

- [Claude Code](https://docs.claude.com/en/docs/claude-code)（或其他支援 skill 的 Claude 用戶端）
- Python 3（建議 3.11），或 [uv](https://github.com/astral-sh/uv)（會自行取得 Python 3.11）
- 首次使用時需要網路，以安裝 QuantLib、pydantic、PyYAML、NumPy 與 pandas

### 方式一：plugin marketplace（建議）

在 Claude Code 中將此儲存庫加入為 marketplace，並安裝 plugin：

```text
/plugin marketplace add kuanHungWang/financial_engineering_skill
/plugin install financial-engineering@financial-engineering-skill
```

或在 shell 中執行相同操作：

```bash
claude plugin marketplace add kuanHungWang/financial_engineering_skill
```

```bash
claude plugin install financial-engineering@financial-engineering-skill
```

重新啟動 Claude Code（或執行 `/reload-plugins`）後，skill 會以 `financial-engineering:structured-derivative-pricer`
出現；當你要求評價商品時，Claude 會自動使用它。之後要更新到新版本：

```bash
claude plugin marketplace update financial-engineering-skill
```

```bash
claude plugin update financial-engineering@financial-engineering-skill
```

### 方式二：複製 skill 資料夾

複製儲存庫，並將 `structured-derivative-pricer/` 資料夾放到 Claude Code 讀取 skill 的位置。

```bash
git clone https://github.com/kuanHungWang/financial_engineering_skill.git
```

供所有專案使用（個人 skill）：

```bash
mkdir -p ~/.claude/skills && cp -R financial_engineering_skill/structured-derivative-pricer ~/.claude/skills/
```

若只想用於單一專案，改複製到該專案的 `.claude/skills/` 資料夾。若希望 `git pull` 就能更新 skill，
也可以改用符號連結（`ln -s`）。

### 方式三：其他 agent

本 skill 遵循 [Agent Skills](https://agentskills.io) 標準，因此也可以用 [skills.sh](https://skills.sh) 等安裝工具
加到其他 coding agent：

```bash
npx skills add kuanHungWang/financial_engineering_skill
```

### 建立 Python 環境（可選）

skill 第一次使用時會自動執行 `scripts/setup.sh`：在 skill 資料夾內建立 `.venv`，安裝 QuantLib 及其他套件，
並印出其 Python 路徑。此腳本具冪等性：重複執行不會有任何變動。若以方式二安裝，想事先完成並檢查安裝：

```bash
PY=$(~/.claude/skills/structured-derivative-pricer/scripts/setup.sh)
$PY -m pytest ~/.claude/skills/structured-derivative-pricer/tests -q
```

---

## 功能

### 1. 建立與驗證評價模型

提供 Term-sheet 或商品描述，請 Claude 計算公平價值、NPV、MTM 或建立評價模型即可。中英文皆可
（例如「結構型商品評價」、「公平價值」、"price this note"）。Claude 會閱讀商品條件、針對模糊之處提問
（barrier 觀察頻率、call 權利由誰持有、fixing 慣例）、轉換你的市場資料，並撰寫交易規格。
引擎在執行前會檢查整份規格並一次回報所有錯誤，接著顯示評價計畫（封閉解或蒙地卡羅及其原因），再進行評價。

#### 1.1 支援資產類別／模型／payoff／early termination

| | 支援內容 |
|---|---|
| **資產類別** | 股票（單一或多個相關標的）· 利率（定期利率、swap rate／CMS、SOFR 等隔夜利率） |
| **模型** | `black_scholes`（固定、期限結構或 Dupire local vol）· `heston`（單一資產，直接給定參數或以波動度曲面校準）· `hull_white` 單因子利率模型（直接給定，或以 ATM swaption 校準：coterminal、完整矩陣或指定點） |
| **觀察值** | 股價水準或績效 · 定期利率 · CMS · 隔夜利率 · `linear` 線性組合（spread、basket、inverse floater 公式）· `min_of` / `max_of`（worst-of、best-of） |
| **Payoff（leg）** | fixed · floating（in advance、in arrears、compounded in arrears with lockout；gearing、spread、cap、floor）· range accrual（任意數量區間、任意觀察值）· option payoff · fixed amount · `path_dependent`（期間之間攜帶狀態：snowball、memory／phoenix coupon、ratchet、cliquet、lookback、cumulative cap）· 任一 leg 上的 kick-in 與 knock-out 條件 |
| **Early termination** | knock-out（autocall）· TARN · Bermudan 履約（issuer call、holder put、cancellation、Bermudan option），以 Longstaff-Schwartz 評價 |
| **封閉解** | European vanilla（BSM、Heston）· 單一 continuous barrier（BSM） |

[`structured-derivative-pricer/examples/`](./structured-derivative-pricer/examples/) 內的範例：
vanilla call、up-and-out call、EKIAKO put（BSM 與 Heston）、phoenix memory autocall、callable snowball、
Bermudan put、callable fixed-rate bond（給定與校準的 Hull-White）、cancellable swap、CMS spread range accrual、
CMS inverse floater in arrears、dual-equity range accrual、先 fixed 後 range accrual 的混合型、
inverse floater TARN、含 lockout 的 SOFR FRN、庫存交易的 range accrual note。

#### 1.2 模型可輸出內容

每個價格都附帶判斷其可信度所需的資訊：

- 評價方法及原因（解析解或 MC）、價格、標準誤與信賴區間，
- 各 leg 現值、knock-in／knock-out／autocall／履約機率、各日期的預期現金流，
- MC 執行且有封閉解適用時，與封閉解的交叉驗證，
- 模擬遠期值相對於曲線遠期值的 martingale check，
- Bermudan 履約的樣本內與樣本外價差，
- Hull-White 校準誤差與 Heston 校準 RMSE，
- 警告訊息，例如波動度曲面外插。

依需求（UAT、模型審查），`price.py inspect` 會以完全相同的方式重新評價（相同種子、相同價格），
並將每個步驟的輸出寫入磁碟：路徑、fixing、現金流、折現因子、履約狀態與迴歸係數，格式為 CSV 與 JSON。

### 2. 撰寫評價驗證報告、解釋評價變異原因

`report.py evidence` 會執行審查者預期的測試（收斂性、種子穩定性、敏感度、壓力測試、基準比較、極限情境、
參考價格），並彙整成素材。Claude 再依此素材撰寫報告，使用你的範本，或
[`templates/sr11-7.md`](./structured-derivative-pricer/templates/sr11-7.md) 中的 SR 11-7 大綱。
所有數字皆從素材原樣複製，絕不由 AI 自行計算。

要解釋價格為何變動時，可在相同種子下比較兩次評價（例如兩個市場資料日期）每個步驟的中間結果，
並以敏感度與壓力測試找出影響價格的輸入。

### 3. 評價庫存交易、重複使用已驗證模型

交易可在存續期間任何一天評價，包含計息期間中途。過去的 fixing 來自共用的 fixings 檔，未來的則來自模擬。
規格只需撰寫一次，之後只要更改評價日與市場資料即可重新評價。

新交易與已驗證交易結構相同時，Claude 會以已驗證的交易檔為起點，只修改 Term-sheet 欄位
（日期、名目本金、履約價、票息、barrier、call 時程），保留模型、校準與評價設定，並在評價前列出差異。

### 4. 匯出模型，無 AI 環境下獨立運作

第一次評價成功後，Claude 會詢問是否要將引擎匯出到你的專案（`export_engine.py`）。此副本不需要 AI，
也不需安裝 skill 即可重新評價：鎖定套件版本（`requirements.lock.txt`）、記錄基準價格（baselines），
並提供 `baseline.sh verify` 偵測重新評價是否不再一致及其原因（輸入變動、檔案被修改、函式庫版本漂移）。
升級引擎時會重新驗證所有基準價格，並封存舊版本。

---

## 限制

以下尚未支援；skill 會直接說明，而不會以近似方式處理：

- 同一交易中有多於一個 Bermudan 履約權（例如同時有 issuer call 與 holder put），
- American（連續）履約（僅在你同意時以每日 Bermudan 履約時程近似），
- 股票加隨機利率的混合模型，
- 外匯、quanto 與多幣別商品，
- `continuous` barrier 的歷史監控（封閉解只從今天往後看；以 MC 評價的每日收盤 barrier 則可正確處理歷史），
- 自訂 payoff 程式碼（規劃中的擴充點）。

其他注意事項：

- **市場資料由你提供。** skill 會轉換你提供的資料（CSV、貼上的表格），但絕不捏造資料。模擬資料僅供示範，
  且回覆中一定會標明。
- **利率選擇權性需要 Hull-White。** 在股票模型下（利率為確定性），cap、floor、利率區間與利率 barrier 會被驗證器拒絕。
- **蒙地卡羅價格帶有誤差。** 請檢查 `std_error`，誤差過大時提高 `num_paths`。
- **報告不涵蓋治理事項。** 核准、評等、負責人與模型清冊等章節只會填入你提供的內容。
- **這是模型，不是投資建議。** 輸出為在所述假設下的模型價值，使用前請依你自己的內控流程驗證。

---

## 評價引擎：QuantLib

本 skill 透過 [QuantLib](https://www.quantlib.org/) 作為評價引擎，以 QuantLib 提供的曲線、營業日曆、計息基礎、
市場慣例、指標利率（`Sofr`、`Euribor`、`EuriborSwapIsdaFixA` 等）、隨機過程與模型校準，以及封閉解計算器為基礎，
建構一個評價計算器封裝層，透過 factory design pattern 與積木堆疊式工作流，直接一個指令定義、串接各元件。
