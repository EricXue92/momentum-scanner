# Adaptive MA Pack + Alerts (TradingView Pine Script v6)

自适应均线组合 + 收盘/实时站上关键价位提醒。叠加在 K 线图上(`overlay=true`)。

## 功能

- **自适应均线**:按当前图表周期(日/周/月)取 EMA 5 / EMA 10 / EMA 20 / SMA 50 / SMA 200;
  15m / 30m / 65m 图上额外画真实的 65 分钟 EMA 20。
- **收盘确认提醒**(30m / 65m 图生效):K 线收盘从下往上站上
  Daily EMA20 / 前日 Low / 前周 Low / 前日 High,含影线跌破后收回的情况;
  每条线每天最多提醒一次(可关)。
- **实时提醒**(任意日内周期):价格盘中穿越前日 Low / 前周 Low / 前日 High,不等收盘。
- **价位线**:前日 Low / High、前周 Low 延伸到右侧并标注价格。
- 可选只在 Gap Down 当天提醒(今日开盘 < 昨日收盘)。

## 提醒设置

| 编号 | 条件 | 频率建议 |
|---|---|---|
| 1) | Bar Close > Daily EMA20 | Once Per Bar Close |
| 2) | Bar Close > Prev Day Low | Once Per Bar Close |
| 3) | Bar Close > Prev Week Low | Once Per Bar Close |
| 6) | Bar Close > Prev Day High | Once Per Bar Close |
| 4) | LIVE Price > Prev Day Low | Once Per Bar |
| 5) | LIVE Price > Prev Week Low | Once Per Bar |
| 7) | LIVE Price > Prev Day High | Once Per Bar |
| 8) | LIVE Price > Daily EMA20 | Once Per Bar |

用 "Any alert() function call" 建一条提醒即可覆盖全部,无需逐条设置。

## 代码

// This Pine Script® code is subject to the terms of the Mozilla Public License 2.0
// https://mozilla.org/MPL/2.0/
// © XueTradingJourney

//@version=6
indicator("Adaptive MA Pack + Alerts", overlay=true)

// =====================================================
// 1. Auto timeframe for main MA pack
// =====================================================
maTf   = timeframe.ismonthly ? "M" : timeframe.isweekly ? "W" : "D"
sameTf = timeframe.isdaily or timeframe.isweekly or timeframe.ismonthly   // 图表周期 == 均线周期

// =====================================================
// 2. Adaptive Moving Averages
//    日线/周线/月线图：直接在本图计算；分钟图：一次请求取齐 5 条
// =====================================================
[sEma5, sEma10, sEma20, sSma50, sSma200] = request.security(syminfo.tickerid, sameTf ? timeframe.period : maTf,
     [ta.ema(close, 5), ta.ema(close, 10), ta.ema(close, 20), ta.sma(close, 50), ta.sma(close, 200)],
     gaps=barmerge.gaps_on, lookahead=barmerge.lookahead_off)

ema5   = sameTf ? ta.ema(close, 5)   : sEma5
ema10  = sameTf ? ta.ema(close, 10)  : sEma10
ema20  = sameTf ? ta.ema(close, 20)  : sEma20
sma50  = sameTf ? ta.sma(close, 50)  : sSma50
sma200 = sameTf ? ta.sma(close, 200) : sSma200

// =====================================================
// 3. True 65-minute EMA 20（只在 15/30/65m 图上请求 65m 数据）
// =====================================================
show65EMA = timeframe.period == "15" or timeframe.period == "30" or timeframe.period == "65"
ema20_65  = request.security(syminfo.tickerid, show65EMA ? "65" : timeframe.period, ta.ema(close, 20),
     gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)

// =====================================================
// 4. Plot Moving Averages
// =====================================================
// EMA 5 - Gray, EMA 10 - Green, EMA 20 - Orange, SMA 50 - Blue, SMA 200 - Black
plot(ema5,   title="EMA 5",   color=color.rgb(90, 90, 90),   linewidth=1)
plot(ema10,  title="EMA 10",  color=color.rgb(55, 175, 75),  linewidth=1)
plot(ema20,  title="EMA 20",  color=color.rgb(255, 145, 0),  linewidth=2)
plot(sma50,  title="SMA 50",  color=color.rgb(40, 100, 255), linewidth=1)
plot(sma200, title="SMA 200", color=color.rgb(20, 20, 20),   linewidth=1)
plot(show65EMA ? ema20_65 : na, title="65m EMA 20", color=color.rgb(160, 70, 255), linewidth=2)

// =====================================================
// 5. Alert levels（日线 / 周线各一次请求取齐）
// =====================================================
// 日线：昨日 EMA20、昨日 Low/High/Close、今日 Open、今日 Low/High
[prevDailyEMA20, prevDayLow, prevDayHigh, prevDayClose, todayOpen, curDayLow, curDayHigh] =
     request.security(syminfo.tickerid, "D",
     [ta.ema(close, 20)[1], low[1], high[1], close[1], open, low, high],
     lookahead=barmerge.lookahead_on)

// 周线：上周 Low、本周 Low
[prevWeekLow, curWeekLow] = request.security(syminfo.tickerid, "W", [low[1], low], lookahead=barmerge.lookahead_on)

// 以当前价格推算的实时 Daily EMA20（与日线图上的 EMA20 一致）
alpha      = 2.0 / (20 + 1)
dailyEMA20 = alpha * close + (1 - alpha) * prevDailyEMA20

// // 固定写法（当天是一条水平线），需要时换成这一行
// dailyEMA20 = prevDailyEMA20

onlyGapDown  = input.bool(false, "只在 Gap Down 当天提醒（今日开盘 < 昨日收盘）")
isGapDownDay = todayOpen < prevDayClose

// =====================================================
// 6. K线收盘从下往上站上（含影线跌破后收回）
// =====================================================
// 在 30m 或 65m 图上生效，按当前图表周期的K线收盘判断
isAlertTf  = timeframe.period == "30" or timeframe.period == "65"
newDay     = timeframe.change("D")
oncePerDay = input.bool(true, "每条线每天最多提醒一次")

gate = isAlertTf and barstate.isconfirmed and (not onlyGapDown or isGapDownDay)

// 之前在线下：本根最低价跌破过线，或者上一根收盘在线下
// 现在在线上：本根收盘 > 线
closeAbove(float lvl) =>
    wasBelow = low < lvl or close[1] <= lvl
    not na(lvl) and wasBelow and close > lvl

rawEMA = gate and closeAbove(dailyEMA20)
rawPDL = gate and closeAbove(prevDayLow)
rawPWL = gate and closeAbove(prevWeekLow)
rawPDH = gate and closeAbove(prevDayHigh)

// 每条线每天最多提醒一次
var bool firedEMA = false
var bool firedPDL = false
var bool firedPWL = false
var bool firedPDH = false

if newDay
    firedEMA := false
    firedPDL := false
    firedPWL := false
    firedPDH := false

crossEMA = rawEMA and not (oncePerDay and firedEMA)
crossPDL = rawPDL and not (oncePerDay and firedPDL)
crossPWL = rawPWL and not (oncePerDay and firedPWL)
crossPDH = rawPDH and not (oncePerDay and firedPDH)

if crossEMA
    firedEMA := true
if crossPDL
    firedPDL := true
if crossPWL
    firedPWL := true
if crossPDH
    firedPDH := true

// 三角标记：橙=Daily EMA20，绿=前日低点，蓝=前周低点，红=前日高点
plotshape(crossEMA, title="Close > Daily EMA20",   style=shape.triangleup, location=location.belowbar, color=color.orange, size=size.tiny)
plotshape(crossPDL, title="Close > Prev Day Low",  style=shape.triangleup, location=location.belowbar, color=color.green,  size=size.tiny)
plotshape(crossPWL, title="Close > Prev Week Low", style=shape.triangleup, location=location.belowbar, color=color.blue,   size=size.tiny)
plotshape(crossPDH, title="Close > Prev Day High", style=shape.triangleup, location=location.belowbar, color=color.red,    size=size.tiny)

// =====================================================
// 6b. 价位线：前日 Low / 前日 High / 前周 Low（延伸到右侧 + 文字 + 价格）
//     收盘后自动切换为今天 / 本周的值
// =====================================================
showLevels = input.bool(true, "显示价位线（Day Low / Day High / Week Low）")

var int dayStartBar = na
if newDay
    dayStartBar := bar_index

// 当前时间是否已过今天 / 本周的收盘时间
dayClosed  = timenow >= time_close("D")
// 本周是否已收盘：今天已收盘且是周五，或者现在是周六/周日
nowDow     = dayofweek(timenow, syminfo.timezone)
barDow     = dayofweek(time,    syminfo.timezone)
weekClosed = (dayClosed and barDow == dayofweek.friday) or nowDow == dayofweek.saturday or nowDow == dayofweek.sunday

// 画线用的价位：收盘后用今天/本周的，否则用昨天/上周的
dispDayLow  = dayClosed  ? curDayLow  : prevDayLow
dispDayHigh = dayClosed  ? curDayHigh : prevDayHigh
dispWeekLow = weekClosed ? curWeekLow : prevWeekLow

var array<line>  lvlLines  = array.new<line>()
var array<label> lvlLabels = array.new<label>()

drawLevel(float price, string txt, color col) =>
    if not na(price) and not na(dayStartBar)
        string lblText = txt + "  " + str.tostring(price, format.mintick)
        array.push(lvlLines, line.new(dayStartBar, price, bar_index + 2, price,
             extend=extend.right, color=col, width=1))
        array.push(lvlLabels, label.new(bar_index + 25, price, lblText,
             style=label.style_none, xloc=xloc.bar_index, yloc=yloc.price,
             textcolor=col, size=size.large, textalign=text.align_right))

if barstate.islast and showLevels and timeframe.isintraday
    for ln in lvlLines
        line.delete(ln)
    for lb in lvlLabels
        label.delete(lb)
    array.clear(lvlLines)
    array.clear(lvlLabels)

    drawLevel(dispDayHigh, "Day High", color.black)

    // Day Low 和 Week Low 相同时只画一条，合并标签
    if dispDayLow == dispWeekLow
        drawLevel(dispDayLow, "Week & Day Low", color.black)
    else
        drawLevel(dispDayLow,  "Day Low",  color.black)
        drawLevel(dispWeekLow, "Week Low", color.black)

// =====================================================
// 7. Alerts（K线收盘确认）
// =====================================================
alertcondition(crossEMA, title="1) Bar Close > Daily EMA20",
     message="{{ticker}} | {{interval}}m CLOSE {{close}} crossed ABOVE Daily 20 EMA")
alertcondition(crossPDL, title="2) Bar Close > Prev Day Low",
     message="{{ticker}} | {{interval}}m CLOSE {{close}} crossed ABOVE Previous Day Low")
alertcondition(crossPWL, title="3) Bar Close > Prev Week Low",
     message="{{ticker}} | {{interval}}m CLOSE {{close}} crossed ABOVE Previous Week Low")
alertcondition(crossPDH, title="6) Bar Close > Prev Day High",
     message="{{ticker}} | {{interval}}m CLOSE {{close}} crossed ABOVE Previous Day High")

if crossEMA or crossPDL or crossPWL or crossPDH
    string msg = syminfo.ticker + " | " + timeframe.period + "m CLOSE " + str.tostring(close) + " crossed ABOVE: "
    msg += crossEMA ? "Daily EMA20 " : ""
    msg += crossPDL ? "Previous Day Low " : ""
    msg += crossPWL ? "Previous Week Low " : ""
    msg += crossPDH ? "Previous Day High " : ""
    alert(msg, alert.freq_once_per_bar_close)


// =====================================================
// 7b. 实时提醒：价格从下往上穿过 Prev Day Low / Prev Week Low / Prev Day High / EMA20 时提醒；
//     穿越后若仍在线上，每隔 N 分钟再提醒一次，最多 M 次；跌回线下则清零，等下一次穿越
//     （用 Any alert() function call 建提醒，频率由代码控制）
// =====================================================
liveGate    = timeframe.isintraday and (not onlyGapDown or isGapDownDay)
repeatMins  = input.int(1, "重复提醒间隔（分钟）",       minval=1, group="实时提醒")
repeatTimes = input.int(3, "每次穿越后最多提醒几次",     minval=1, group="实时提醒")
repeatMs    = repeatMins * 60 * 1000

// 穿越判断（每根K线都计算，避免 CW10002 警告）
rawLivePDL = ta.crossover(close, prevDayLow)
rawLivePWL = ta.crossover(close, prevWeekLow)
rawLivePDH = ta.crossover(close, prevDayHigh)
rawLiveE20 = ta.crossover(close, dailyEMA20)

// 每条线记录"上次提醒时间"和"已提醒次数"；跌回线下时清零
varip int lastPDL = na
varip int lastPWL = na
varip int lastPDH = na
varip int lastE20 = na
varip int cntPDL  = 0
varip int cntPWL  = 0
varip int cntPDH  = 0
varip int cntE20  = 0

// 返回 [是否提醒, 新的上次时间, 新的次数]
// crossed：这一刻是否从下往上穿过线；above：当前价格是否在线上
shouldAlert(bool crossed, bool above, int lastT, int cnt) =>
    bool fire = false
    int  newT = lastT
    int  newC = cnt
    if crossed
        // 刚穿越：立即提醒，开始计数
        fire := true
        newT := timenow
        newC := 1
    else if above and newC > 0
        // 穿越之后仍在线上：按间隔重复，最多 repeatTimes 次
        if newC < repeatTimes and timenow - lastT >= repeatMs
            fire := true
            newT := timenow
            newC += 1
    else if not above
        // 跌回线下：清零，等下一次穿越
        newT := na
        newC := 0
    [fire, newT, newC]

live = liveGate and barstate.isrealtime
[fPDL, nPDL, cPDL] = shouldAlert(live and rawLivePDL, close > prevDayLow,  lastPDL, cntPDL)
[fPWL, nPWL, cPWL] = shouldAlert(live and rawLivePWL, close > prevWeekLow, lastPWL, cntPWL)
[fPDH, nPDH, cPDH] = shouldAlert(live and rawLivePDH, close > prevDayHigh, lastPDH, cntPDH)
[fE20, nE20, cE20] = shouldAlert(live and rawLiveE20, close > dailyEMA20, lastE20, cntE20)
lastPDL := nPDL
lastPWL := nPWL
lastPDH := nPDH
lastE20 := nE20
cntPDL  := cPDL
cntPWL  := cPWL
cntPDH  := cPDH
cntE20  := cE20

// 4)/5)/7) 单独提醒（穿越那一刻触发，不重复）
alertcondition(liveGate and rawLivePDL, title="4) LIVE Price > Prev Day Low",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Day Low (live)")
alertcondition(liveGate and rawLivePWL, title="5) LIVE Price > Prev Week Low",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Week Low (live)")
alertcondition(liveGate and rawLivePDH, title="7) LIVE Price > Prev Day High",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Day High (live)")
alertcondition(liveGate and rawLiveE20, title="8) LIVE Price > Daily EMA20",
     message="{{ticker}} | Price {{close}} crossed ABOVE Daily 20 EMA (live)")

// 合并实时提醒（穿越触发 + 有限次重复）
if fPDL or fPWL or fPDH or fE20
    string liveMsg = syminfo.ticker + " | LIVE " + str.tostring(close) + " crossed ABOVE: "
    liveMsg += fPDL ? "Previous Day Low " : ""
    liveMsg += fPWL ? "Previous Week Low " : ""
    liveMsg += fPDH ? "Previous Day High " : ""
    liveMsg += fE20 ? "Daily EMA20 " : ""
    alert(liveMsg, alert.freq_all)
    
// =====================================================
// 8. RVOL at Time（开盘累计量 ÷ 过去 N 天同一时刻的平均累计量）
//    只在分钟图上计算和显示，日线以上自动跳过
// =====================================================
rvolShow = input.bool(true,  "显示 RVOL(t)",                 group="RVOL AT TIME")
rvolDays = input.int(20,     "回看天数", minval=1, maxval=60, group="RVOL AT TIME")
rvolMax  = input.int(400,    "每天最大K线数（1m 常规=390，含盘前后=960）", minval=50, group="RVOL AT TIME")
rvolHi   = input.float(2.0,  "高亮阈值（倍）", step=0.1,      group="RVOL AT TIME")
rvolPos  = input.string("top_right", "表格位置", options=["top_right", "top_left", "bottom_right", "bottom_left"], group="RVOL AT TIME")
rvolSize = input.string("normal", "字体大小", options=["small", "normal", "large"], group="RVOL AT TIME")

rvolOn = rvolShow and timeframe.isintraday

var int   rvDayStart = na
var float rvCum      = 0.0
var array<float>  rvToday = array.new<float>(rvolMax, na)
var matrix<float> rvHist  = matrix.new<float>(0, rvolMax, na)

if rvolOn and newDay
    // 把昨天的累计曲线存进历史，只保留最近 rvolDays 天
    if rvCum > 0
        matrix.add_row(rvHist, matrix.rows(rvHist), array.copy(rvToday))
        if matrix.rows(rvHist) > rvolDays
            matrix.remove_row(rvHist, 0)
    rvToday    := array.new<float>(rvolMax, na)
    rvCum      := 0.0
    rvDayStart := time

if rvolOn
    rvCum += volume

// 用"距开盘多少根K线"定位时间槽，缺K线也不会错位
int rvSlot = (not rvolOn or na(rvDayStart)) ? -1 : int((time - rvDayStart) / (timeframe.in_seconds() * 1000))
if rvSlot >= 0 and rvSlot < rvolMax
    array.set(rvToday, rvSlot, rvCum)

// 过去 N 天同一时刻的平均累计量
float rvAvg = na
if rvSlot >= 0 and rvSlot < rvolMax and matrix.rows(rvHist) > 0
    float rvS = 0.0
    int   rvN = 0
    for x in matrix.col(rvHist, rvSlot)
        if not na(x)
            rvS += x
            rvN += 1
    rvAvg := rvN > 0 ? rvS / rvN : na

rvol     = rvCum / rvAvg
rvolMins = (rvSlot + 1) * timeframe.in_seconds() / 60

tblPos = rvolPos == "top_left"     ? position.top_left     :
         rvolPos == "bottom_right" ? position.bottom_right :
         rvolPos == "bottom_left"  ? position.bottom_left  : position.top_right
tblSz  = rvolSize == "small" ? size.small : rvolSize == "large" ? size.large : size.normal

var table rvTbl = table.new(tblPos, 2, 1, bgcolor=color.new(color.black, 20))
if barstate.islast and rvolOn
    string rvolTxt = na(rvol) ? "n/a" : str.tostring(rvol, "0.00") + "x  (" + str.tostring(rvolMins, "0") + "m)"
    color  rvolCol = na(rvol) ? color.gray : rvol >= rvolHi ? color.lime : rvol < 1.0 ? color.red : color.white
    table.cell(rvTbl, 0, 0, "RVOL(t): ", text_color=color.white, text_size=tblSz, text_halign=text.align_right)
    table.cell(rvTbl, 1, 0, rvolTxt,     text_color=rvolCol,     text_size=tblSz, text_halign=text.align_right)
