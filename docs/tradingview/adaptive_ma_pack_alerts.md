# Adaptive MA Pack + Alerts (TradingView Pine Script v6)

自适应均线组合 + 收盘/实时站上关键价位提醒 + RVOL at Time。叠加在 K 线图上(`overlay=true`)。

## 功能

- **自适应均线**:按当前图表周期(日/周/月)取 EMA 5 / EMA 10 / EMA 20 / SMA 50 / SMA 200;
  15m / 30m / 65m 图上额外画真实的 65 分钟 EMA 20。
- **收盘确认提醒**(30m / 65m 图生效):K 线收盘从下往上站上
  Daily EMA20 / 前日 Low / 前周 Low / 前日 High,含影线跌破后收回的情况;
  每条线每天最多提醒一次(可关)。同一根 K 线站上多条线时合并成一条消息,用 `&` 分隔。
- **实时提醒**(任意日内周期):价格盘中从下往上穿过前日 Low / 前周 Low / 前日 High /
  昨日 Daily EMA20,不等收盘;同一根 K 线内跌破再收回、跳空高开都算。
  穿越后若仍在线上,每隔 N 分钟重复一次,每次穿越最多 M 次;
  每条线每天总共最多 `dayCap` 次(默认都是 3)。
- **价位线**(日内周期):前日 Low / High、前周 Low 延伸到右侧并标注价格;
  收盘后自动切换为今天 / 本周的值;Day Low 与 Week Low 相同时合并成一条。
- **RVOL(t)**:开盘以来的累计成交量 ÷ 过去 N 天(默认 20)同一分钟的平均累计量,
  固定显示在右下角,见下一节。
- 可选只在 Gap Down 当天提醒(今日开盘 < 昨日收盘)。

## RVOL(t) 口径

- 用 `request.security_lower_tf` 取 1 分钟量,1 分钟 ~ 单日线图上都能算;其他周期显示 `n/a`。
- **只算常规时段的连续交易**(美股 09:30–15:59,共 390 个分钟槽)。
  16:00 的收盘集合竞价、盘前盘后成交都不计入。
- 显示格式 `0.63x (390m)`:括号里是已累计的分钟数。≥ 高亮阈值(默认 2.0)为绿色,< 1.0 为红色。
- **收盘后的数值和「日线量 ÷ 均量」对不上是正常的**:日线量包含收盘竞价、盘后成交和大宗成交,
  而 TradingView 的分钟 K 线不含其中一部分。当天有大笔盘后或场外成交时两者会明显背离
  (例:2026-11-16 CAKE,日线量 +7%,RVOL(t) 0.63x)。盘中判断交易热度以 RVOL(t) 为准。
- 「调试:显示每日累计量」开关会在最后一根 K 线右侧打出今天和历史每天的累计量,用于核对数据,平时关闭。

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

用 "Any alert() function call" 建一条提醒即可覆盖全部,无需逐条设置;
实时提醒的重复间隔和次数上限由代码里的「实时提醒」参数控制。

## 代码

```pine
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

// 合并的收盘确认提醒（多条线用 & 分隔）
if crossEMA or crossPDL or crossPWL or crossPDH
    array<string> parts = array.new<string>()
    if crossEMA
        array.push(parts, "Daily EMA20")
    if crossPDL
        array.push(parts, "Previous Day Low")
    if crossPWL
        array.push(parts, "Previous Week Low")
    if crossPDH
        array.push(parts, "Previous Day High")
    string msg = syminfo.ticker + " | " + timeframe.period + "m CLOSE " + str.tostring(close) + " crossed ABOVE: " + array.join(parts, " & ")
    alert(msg, alert.freq_once_per_bar_close)

// =====================================================
// 7b. 实时提醒：价格从下往上穿过 Prev Day Low / Prev Week Low / Prev Day High / EMA20 时提醒
//     （含同一根K线内跌破再收回、跳空高开）；
//     穿越后若仍在线上，每隔 N 分钟再提醒一次，最多 M 次；
//     每条线每天总共最多提醒 dayCap 次；两次提醒之间至少间隔 N 分钟
//     （用 Any alert() function call 建提醒，频率由代码控制）
// =====================================================
liveGate    = timeframe.isintraday and (not onlyGapDown or isGapDownDay)
repeatMins  = input.int(1, "重复提醒间隔（分钟）",             minval=1, group="实时提醒")
repeatTimes = input.int(3, "每次穿越后最多提醒几次",           minval=1, group="实时提醒")
dayCap      = input.int(3, "每条线每天最多提醒几次（总上限）", minval=1, group="实时提醒")
repeatMs    = repeatMins * 60 * 1000

// 固定价位：上一根收盘在"今天这条线"之下、现在在线上（跳空高开第一根也能识别）
crossUpLvl(float lvl) => not na(lvl) and close > lvl and close[1] <= lvl

rawLivePDL = crossUpLvl(prevDayLow)
rawLivePWL = crossUpLvl(prevWeekLow)
rawLivePDH = crossUpLvl(prevDayHigh)
rawLiveE20 = crossUpLvl(prevDailyEMA20)

// 每条线记录"上次提醒时间"、"本轮状态/次数"、"当日累计次数"
// cnt 含义：-1 = 价格在线下（待穿越）；0 = 在线上但本轮未触发；>0 = 本轮已提醒次数
varip int lastPDL = na
varip int lastPWL = na
varip int lastPDH = na
varip int lastE20 = na
varip int cntPDL  = 0
varip int cntPWL  = 0
varip int cntPDH  = 0
varip int cntE20  = 0
varip int dayPDL  = 0
varip int dayPWL  = 0
varip int dayPDH  = 0
varip int dayE20  = 0

// 换日：当日累计清零；本轮状态也清零（线换成了新值，要按新线重新判断）
if newDay and barstate.isnew
    dayPDL := 0
    dayPWL := 0
    dayPDH := 0
    dayE20 := 0
    cntPDL := 0
    cntPWL := 0
    cntPDH := 0
    cntE20 := 0

// 返回 [是否提醒, 新的上次时间, 新的本轮次数, 新的当日累计]
shouldAlert(bool isLive, bool crossed, bool above, int lastT, int cnt, int dayCnt) =>
    bool fire = false
    int  newT = lastT
    int  newC = cnt
    int  newD = dayCnt
    if not above
        // 价格在线下：进入待穿越状态
        newC := -1
    else if not isLive
        // 历史K线：只记录状态，不提醒
        newC := 0
    else if newD >= dayCap
        // 今天已达上限：不再提醒
        fire := false
    else if na(lastT) or timenow - lastT >= repeatMs
        if newC == -1 or (crossed and newC == 0)
            // 从线下回到线上（含同一根K线内跌破再收回、跳空高开）
            fire := true
            newT := timenow
            newC := 1
            newD += 1
        else if newC > 0 and newC < repeatTimes
            // 穿越后仍在线上：按间隔重复
            fire := true
            newT := timenow
            newC += 1
            newD += 1
    [fire, newT, newC, newD]

live = liveGate and barstate.isrealtime
[fPDL, nPDL, cPDL, dPDL] = shouldAlert(live, rawLivePDL, close > prevDayLow,     lastPDL, cntPDL, dayPDL)
[fPWL, nPWL, cPWL, dPWL] = shouldAlert(live, rawLivePWL, close > prevWeekLow,    lastPWL, cntPWL, dayPWL)
[fPDH, nPDH, cPDH, dPDH] = shouldAlert(live, rawLivePDH, close > prevDayHigh,    lastPDH, cntPDH, dayPDH)
[fE20, nE20, cE20, dE20] = shouldAlert(live, rawLiveE20, close > prevDailyEMA20, lastE20, cntE20, dayE20)
lastPDL := nPDL
lastPWL := nPWL
lastPDH := nPDH
lastE20 := nE20
cntPDL  := cPDL
cntPWL  := cPWL
cntPDH  := cPDH
cntE20  := cE20
dayPDL  := dPDL
dayPWL  := dPWL
dayPDH  := dPDH
dayE20  := dE20

// 单独提醒（按K线判断的穿越，不重复）
alertcondition(liveGate and rawLivePDL, title="4) LIVE Price > Prev Day Low",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Day Low (live)")
alertcondition(liveGate and rawLivePWL, title="5) LIVE Price > Prev Week Low",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Week Low (live)")
alertcondition(liveGate and rawLivePDH, title="7) LIVE Price > Prev Day High",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Day High (live)")
alertcondition(liveGate and rawLiveE20, title="8) LIVE Price > Daily EMA20",
     message="{{ticker}} | Price {{close}} crossed ABOVE Daily 20 EMA (live)")

// 合并实时提醒（多条线用 & 分隔）
if fPDL or fPWL or fPDH or fE20
    array<string> liveParts = array.new<string>()
    if fE20
        array.push(liveParts, "Daily EMA20")
    if fPDL
        array.push(liveParts, "Previous Day Low")
    if fPWL
        array.push(liveParts, "Previous Week Low")
    if fPDH
        array.push(liveParts, "Previous Day High")
    string liveMsg = syminfo.ticker + " | LIVE " + str.tostring(close) + " crossed ABOVE: " + array.join(liveParts, " & ")
    alert(liveMsg, alert.freq_all)

// =====================================================
// 8. RVOL at Time（1 分钟粒度，任何周期图上都显示，固定右下角）
//    开盘累计量 ÷ 过去 N 天同一分钟的平均累计量；只算常规时段
// =====================================================
rvolShow = input.bool(true,  "显示 RVOL(t)",                     group="RVOL AT TIME")
rvolDays = input.int(20,     "回看天数", minval=1, maxval=60,     group="RVOL AT TIME")
rvolMax  = input.int(390,    "常规时段分钟数（美股 390）", minval=60, group="RVOL AT TIME")
openHour = input.int(9,      "常规开盘 时", minval=0, maxval=23,  group="RVOL AT TIME")
openMin  = input.int(30,     "常规开盘 分", minval=0, maxval=59,  group="RVOL AT TIME")
rvolHi   = input.float(2.0,  "高亮阈值（倍）", step=0.1,          group="RVOL AT TIME")
rvolSize = input.string("normal", "字体大小", options=["small", "normal", "large"], group="RVOL AT TIME")

// 1 分钟 ~ 单日线图上计算；其他周期显示 n/a
rvolOn = (timeframe.isintraday and timeframe.in_seconds() >= 60) or (timeframe.isdaily and timeframe.multiplier == 1)

// 当前图表K线内的 1 分钟成交量、开始时间、结束时间（一次请求）
[vol1m, time1m, close1m] = request.security_lower_tf(syminfo.tickerid, "1", [volume, time, time_close], ignore_invalid_timeframe=true)

// 1 分钟K线相对常规开盘的分钟数（交易所时区）；盘前为负，盘后 >= rvolMax
minuteSlot(int t) =>
    hour(t, syminfo.timezone) * 60 + minute(t, syminfo.timezone) - (openHour * 60 + openMin)

var int   rvDay      = na                            // 当前累计属于哪一天
var int   rvLastSlot = -1                            // 当天最后一个有效分钟槽（盘后沿用）
var float rvCumBase  = 0.0                           // 本根K线开始前的当日累计量
var array<float>  rvToday = array.new<float>(rvolMax, na)
var matrix<float> rvHist  = matrix.new<float>(0, rvolMax, na)

// 换日：按交易所时区的日期判断
int todayKey = year(time, syminfo.timezone) * 10000 + month(time, syminfo.timezone) * 100 + dayofmonth(time, syminfo.timezone)
if rvolOn and (na(rvDay) or todayKey != rvDay)
    // 昨天有数据且从开盘第 1 分钟起有记录，才存入历史
    if rvCumBase > 0 and not na(array.get(rvToday, 0))
        matrix.add_row(rvHist, matrix.rows(rvHist), array.copy(rvToday))
        if matrix.rows(rvHist) > rvolDays
            matrix.remove_row(rvHist, 0)
    rvToday    := array.new<float>(rvolMax, na)
    rvCumBase  := 0.0
    rvLastSlot := -1
    rvDay      := todayKey

// 把本根K线里常规时段、且已结束的 1 分钟量累加进当天的分钟槽
float rvCum  = rvCumBase
int   rvSlot = rvLastSlot
if rvolOn and array.size(vol1m) > 0
    for k = 0 to array.size(vol1m) - 1
        int  s     = minuteSlot(array.get(time1m, k))
        // 历史K线全算；实时K线只算已走完的分钟；K线收盘时其中所有分钟都算已结束
        bool ended = not barstate.isrealtime or barstate.isconfirmed or array.get(close1m, k) <= timenow
        if ended and s >= 0 and s < rvolMax
            rvCum += nz(array.get(vol1m, k))
            array.set(rvToday, s, rvCum)
            rvSlot := s
if barstate.isconfirmed
    rvCumBase  := rvCum
    rvLastSlot := rvSlot

// 过去 N 天同一分钟的平均累计量
float rvAvg = na
if rvolOn and rvSlot >= 0 and matrix.rows(rvHist) > 0
    float rvS = 0.0
    int   rvN = 0
    for x in matrix.col(rvHist, rvSlot)
        if not na(x)
            rvS += x
            rvN += 1
    rvAvg := rvN > 0 ? rvS / rvN : na

rvol     = rvAvg > 0 ? rvCum / rvAvg : na
rvolMins = rvSlot + 1

// ---------- 调试：今天和历史每天在当前分钟槽的累计量（默认关闭） ----------
rvDebug = input.bool(false, "调试：显示每日累计量", group="RVOL AT TIME")
var label rvDbgLbl = na
if barstate.islast and rvDebug and rvSlot >= 0
    string dbg = "today " + str.tostring(rvCum, "#") + " | avg " + str.tostring(rvAvg, "#") + " | days " + str.tostring(matrix.rows(rvHist)) + " | slot " + str.tostring(rvSlot)
    if matrix.rows(rvHist) > 0
        for r = 0 to matrix.rows(rvHist) - 1
            dbg += "\n" + str.tostring(r + 1) + ": " + str.tostring(matrix.get(rvHist, r, rvSlot), "#")
    label.delete(rvDbgLbl)
    rvDbgLbl := label.new(bar_index, high, dbg, style=label.style_label_left, color=color.new(color.black, 20), textcolor=color.white, size=size.small, textalign=text.align_left)

// ---------- 表格：固定右下角，始终显示 ----------
tblSz = rvolSize == "small" ? size.small : rvolSize == "large" ? size.large : size.normal
var table rvTbl = table.new(position.bottom_right, 2, 1, bgcolor=color.new(color.black, 20))
if barstate.islast and rvolShow
    string rvolTxt = na(rvol) ? "n/a" : str.tostring(rvol, "0.00") + "x  (" + str.tostring(rvolMins, "0") + "m)"
    color  rvolCol = na(rvol) ? color.gray : rvol >= rvolHi ? color.lime : rvol < 1.0 ? color.red : color.white
    table.cell(rvTbl, 0, 0, "RVOL(t): ", text_color=color.white, text_size=tblSz, text_halign=text.align_right)
    table.cell(rvTbl, 1, 0, rvolTxt,     text_color=rvolCol,     text_size=tblSz, text_halign=text.align_right)
```
