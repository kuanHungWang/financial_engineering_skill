[English](./README.md) | [繁體中文](./README.zh-TW.md)

# 結構型商品評價器（Structured Derivative Pricer）— Claude 技能

一個 [Claude 技能（skill）](https://docs.claude.com/en/docs/claude-code/skills)：依據條款說明書（term sheet）評價
奇異選擇權與結構型商品，並為已評價的交易撰寫模型文件／模型驗證報告（SR 11-7 格式）。

**Claude 只撰寫宣告式的 YAML 交易規格，絕不撰寫評價程式碼。** 由預先建置並經過測試的 QuantLib 引擎驗證規格、
選擇評價方法（有封閉解時用封閉解，否則用蒙地卡羅模擬）並完成評價。在固定亂數種子下價格可完全重現，
且不依賴 AI 臨時寫出的程式碼。

```
你：「幫我評價這張自動贖回票券」+ 條款說明書 + 市場資料
        │
        ▼
Claude：閱讀條款、針對模糊之處提問、轉換你的市場資料、
        撰寫 trade_<name>.yaml
        │
        ▼
引擎：validate（驗證）─▶ plan（解析解或 MC 及其原因）─▶ price（評價）─▶ result.json
```

---

## 安裝

### 需求

- [Claude Code](https://docs.claude.com/en/docs/claude-code)（或其他支援技能的 Claude 用戶端）
- Python 3.11（或 [uv](https://github.com/astral-sh/uv)，它會自行取得正確版本的 Python）
- 首次使用時需要網路，以安裝 QuantLib、pydantic、PyYAML、NumPy 與 pandas

### 安裝技能

複製儲存庫，並將 `structured-derivative-pricer/` 資料夾放到 Claude Code 讀取技能的位置。

```bash
git clone https://github.com/kuanHungWang/financial_engineering_skill.git
```

供所有專案使用（個人技能）：

```bash
mkdir -p ~/.claude/skills && cp -R financial_engineering_skill/structured-derivative-pricer ~/.claude/skills/
```

若只想用於單一專案，改複製到該專案的 `.claude/skills/` 資料夾。若希望 `git pull` 就能更新技能，
也可以改用符號連結（`ln -s`）。

### 建立 Python 環境（可選）

技能第一次使用時會自動執行此步驟。若想事先完成：

```bash
~/.claude/skills/structured-derivative-pricer/scripts/setup.sh
```

它會建立 `structured-derivative-pricer/.venv`，安裝 QuantLib 及其他套件，並印出其 Python 路徑。
此腳本具冪等性：重複執行不會有任何變動。

### 檢查安裝

```bash
PY=$(~/.claude/skills/structured-derivative-pricer/scripts/setup.sh)
$PY -m pytest ~/.claude/skills/structured-derivative-pricer/tests -q
```

### 試用

[`test_case/`](./test_case/README.md) 資料夾內有七份自然語言撰寫的條款說明書，以及原始模擬市場資料
（CSV，刻意不採用技能的格式）。將它複製到專案中，然後對 Claude 說，例如：

> 評價 `termsheets/03_worst_of_phoenix_autocall.md`，市場資料在 `market_data/`。

> 幫它寫一份 SR 11-7 模型驗證報告。

---

## 功能

### 評價

提供條款說明書或商品描述，請 Claude 計算公平價值、NPV、MTM 或建立評價模型即可。中英文皆可
（例如「結構型商品評價」、「公平價值」、"price this note"）。

商品由積木式元件組裝而成，因此大多數條款不需要新程式碼：

| 元件 | 支援內容 |
|---|---|
| **模型** | `black_scholes`（單一或多個相關股票；固定、期限結構或 Dupire 局部波動度）· `heston`（單一資產，直接給定參數或以波動度曲面校準）· `hull_white` 單因子利率模型（直接給定，或以 ATM swaption 校準：coterminal、完整矩陣或指定點） |
| **觀察值** | 股價水準或績效 · 定期利率 · 交換利率（CMS）· 隔夜利率（SOFR）· `linear` 線性組合（利差、一籃子、反浮動公式）· `min_of` / `max_of`（最差／最佳表現） |
| **現金流腿（Legs）** | 固定 · 浮動（期初定價、期末定價、期末複利含 lockout；槓桿、加碼、上限、下限）· 區間計息（任意數量區間、任意觀察值）· 選擇權報酬 · 固定金額 · `path_dependent`（期間之間攜帶狀態：雪球、記憶型／鳳凰票息、棘輪、cliquet、回顧型、累計上限） |
| **條件遮罩／事件** | 任一腿上的 kick-in 與 knock-out 條件 · 交易層級 knock-out（自動贖回）· 目標贖回（TARN）· 百慕達式履約（發行人買回、持有人賣回、可取消、百慕達選擇權），以 Longstaff-Schwartz 評價 |
| **封閉解** | 歐式陽春選擇權（BSM、Heston）· 單一連續障礙（BSM） |

[`structured-derivative-pricer/examples/`](./structured-derivative-pricer/examples/) 內的範例：
陽春買權、上出局買權、EKIAKO 賣權（BSM 與 Heston）、鳳凰記憶型自動贖回、可贖回雪球、百慕達賣權、
可贖回固定利率債券（給定與校準的 Hull-White）、可取消交換、CMS 利差區間計息、CMS 期末定價反浮動、
雙股票區間計息、先固定後區間計息混合型、反浮動 TARN、含 lockout 的 SOFR FRN、已存續區間計息票券。

### 已存續交易（Seasoned trades）

交易可在存續期間任何一天評價，包含計息期間中途。過去的定價值（fixings）來自共用的 fixings 檔，
未來的則來自模擬。規格只需撰寫一次，之後只要更改評價日與市場資料即可重新評價。

### 結果檢查

每個價格都附帶判斷其可信度所需的資訊：

- 評價方法及原因（解析解或 MC）、價格、標準誤與信賴區間，
- 各腿現值、kick-in／knock-out／自動贖回／履約機率、各日期的預期現金流，
- MC 執行且有封閉解適用時，與封閉解的交叉驗證，
- 模擬遠期值相對於曲線遠期值的平賭檢定（martingale check），
- 百慕達式履約的樣本內與樣本外價差，
- Hull-White 校準誤差與 Heston 校準 RMSE，
- 警告訊息，例如波動度曲面外插。

### 中間結果（UAT、模型審查）

`price.py inspect` 會以完全相同的方式重新評價（相同種子、相同價格），並將計算圖中每個節點的輸出寫入磁碟：
路徑、定價值、現金流、折現因子、履約狀態與迴歸係數，格式為 CSV 與 JSON。適合手動核對現金流或檢查履約邊界。

### 模型驗證報告

依需求，`report.py evidence` 會執行審查者預期的測試（收斂性、種子穩定性、敏感度、壓力測試、基準比較、
極限情境、參考價格），並彙整成素材。Claude 再依此素材撰寫報告，使用你的範本，或
[`templates/sr11-7.md`](./structured-derivative-pricer/templates/sr11-7.md) 中的 SR 11-7 大綱。
所有數字皆從素材原樣複製，絕不由 AI 自行計算。

### 獨立副本

第一次評價成功後，Claude 會詢問是否要將引擎匯出到你的專案（`export_engine.py`）。此副本不需要 AI，
也不需安裝技能即可重新評價：鎖定套件版本（`requirements.lock.txt`）、記錄基準價格（baselines），
並提供 `baseline.sh verify` 偵測重新評價是否不再一致及其原因（檔案被修改、函式庫版本漂移）。
升級引擎時會重新驗證所有基準價格，並封存舊版本。

---

## 限制

以下尚未支援；技能會直接說明，而不會以近似方式處理：

- 同一交易中有多於一個百慕達式履約權（例如同時有發行人買回與持有人賣回），
- 美式（連續）履約（僅在你同意時以每日百慕達履約時程近似），
- 股票加隨機利率的混合模型，
- 外匯、quanto 與多幣別商品，
- `continuous` 連續障礙的歷史監控（封閉解只從今天往後看；以 MC 評價的每日收盤障礙則可正確處理歷史），
- 自訂報酬程式碼（規劃中的擴充點）。

其他注意事項：

- **市場資料由你提供。** 技能會轉換你提供的資料（CSV、貼上的表格），但絕不捏造資料。模擬資料僅供示範，
  且回覆中一定會標明。
- **利率選擇權性需要 Hull-White。** 在股票模型下（利率為確定性），上限、下限、利率區間與利率障礙會被驗證器拒絕。
- **蒙地卡羅價格帶有誤差。** 請檢查 `std_error`，誤差過大時提高 `num_paths`。
- **報告不涵蓋治理事項。** 核准、評等、負責人與模型清冊等章節只會填入你提供的內容。
- **這是模型，不是投資建議。** 輸出為在所述假設下的模型價值，使用前請依你自己的內控流程驗證。

---

## 背後的引擎

### 規格 → 計算圖 → 價格

引擎採工廠式設計：將 YAML 規格轉為由預先測試元件構成的計算圖，再執行它。

```
curve ─▶ discount ─▶ underlying ─▶ model ─▶ scenario ─▶ observables ─▶ legs ─▶ masks ─▶ price
                                                   └────────▶ discount_factors ─────────┘
```

1. **驗證**（`price.py validate`）：執行前以 pydantic schema 檢查整份規格。所有錯誤一次回報，
   附上輸入路徑與提示（例如 "Did you mean ..."）。
2. **規劃**（`price.py plan`）：顯示計算圖、定價日期，以及選定的方法與原因。
3. **評價**（`price.py price`）：有對應封閉解時使用封閉解，否則使用蒙地卡羅（對偶路徑、固定種子），
   百慕達式履約則使用 Longstaff-Schwartz 迴歸。

### 以 QuantLib 為基礎

[QuantLib](https://www.quantlib.org/) 提供殖利率曲線、營業日曆、計息基礎、時程、指標定義
（`Sofr`、`Euribor`、`EuriborSwapIsdaFixA` 等）、Black-Scholes／Heston／Hull-White 隨機過程及其校準，
以及封閉解引擎。本評價器在其上加入可組裝的報酬層、路徑模擬、可讀取歷史的觀察值，以及履約邏輯。

### 目錄結構

```
structured-derivative-pricer/
├── SKILL.md            Claude 遵循的指示
├── pricer/             引擎本體（registry、spec、models、observables、legs、masks、Bermudan、analytic 等）
├── scripts/            price.py、report.py、export_engine.py、baseline.py、make_mock_market.py、setup.sh
├── reference/          元件、市場資料格式、履約、模型報告指南
├── templates/          SR 11-7 報告大綱
├── examples/           範例交易規格 + 模擬市場資料與 fixings
└── tests/              以封閉解與極限情境為基準的回歸測試
docs/index.html         靜態元件參考頁（以瀏覽器開啟）
test_case/              冒煙測試用條款說明書與原始模擬市場資料
```

### 指令

```bash
PY=$(structured-derivative-pricer/scripts/setup.sh)
$PY structured-derivative-pricer/scripts/price.py catalog [kind]       # 所有元件、欄位、預設值
$PY structured-derivative-pricer/scripts/price.py validate trade.yaml
$PY structured-derivative-pricer/scripts/price.py plan trade.yaml
$PY structured-derivative-pricer/scripts/price.py price trade.yaml --out result.json --set pricing.num_paths=100000
$PY structured-derivative-pricer/scripts/price.py inspect trade.yaml --out-dir diagnostics/inspect
```

最小規格範例（[`examples/vanilla_call.yaml`](./structured-derivative-pricer/examples/vanilla_call.yaml)）：

```yaml
valuation_date: 2026-09-28
market_data: market/mock_market.yaml
model: {type: black_scholes, underlying: SPX, discount_curve: USD}
trade:
  id: CALL-SPX-1Y
  maturity: 1Y
  legs:
    - {id: call, type: option_payoff, underlying: SPX, option_type: call, strike: 1.0, notional: 1000000}
```

### 擴充

每個元件都是以 `@register(kind, type)` 註冊、並帶有 pydantic `Spec` 的類別（`pricer/registry.py`）。
新增觀察值、腿、條件、事件、模型或解析評價器只需一個類別；驗證、catalog 與計算圖會自動納入。
請在 `tests/` 中加入以封閉解或極限情境為基準的回歸測試。新增元件後，以 `$PY docs/build_catalog.py`
更新文件頁。詳見 [`SKILL.md`](./structured-derivative-pricer/SKILL.md) 的 "Extending" 一節。
