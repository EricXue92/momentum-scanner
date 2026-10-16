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

用 "Any alert() function call" 建一条提醒即可覆盖全部,无需逐条设置。

## 代码

```pinescript
// This Pine Script® code is subject to the terms of the Mozilla Public License 2.0
// https://mozilla.org/MPL/2.0/
// © XueTradingJourney

//@version=6
indicator("Adaptive MA Pack + Alerts", overlay=true)

// =====================================================
// 1. Auto timeframe for main MA pack
// =====================================================
maTf = timeframe.ismonthly ? "M" : timeframe.isweekly ? "W" : "D"

// =====================================================
// 2. Adaptive Moving Averages
// =====================================================
ema5   = request.security(syminfo.tickerid, maTf, ta.ema(close, 5),   gaps=barmerge.gaps_on, lookahead=barmerge.lookahead_off)
ema10  = request.security(syminfo.tickerid, maTf, ta.ema(close, 10),  gaps=barmerge.gaps_on, lookahead=barmerge.lookahead_off)
ema20  = request.security(syminfo.tickerid, maTf, ta.ema(close, 20),  gaps=barmerge.gaps_on, lookahead=barmerge.lookahead_off)
sma50  = request.security(syminfo.tickerid, maTf, ta.sma(close, 50),  gaps=barmerge.gaps_on, lookahead=barmerge.lookahead_off)
sma200 = request.security(syminfo.tickerid, maTf, ta.sma(close, 200), gaps=barmerge.gaps_on, lookahead=barmerge.lookahead_off)

// =====================================================
// 3. True 65-minute EMA 20
// =====================================================
ema20_65  = request.security(syminfo.tickerid, "65", ta.ema(close, 20), gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)
show65EMA = timeframe.period == "15" or timeframe.period == "30" or timeframe.period == "65"

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
// 5. Alert levels
// =====================================================
// 昨天收盘时确定的 Daily EMA20
prevDailyEMA20 = request.security(syminfo.tickerid, "D", ta.ema(close, 20)[1], lookahead=barmerge.lookahead_on)
// 以当前价格推算的实时 Daily EMA20(与日线图上的 EMA20 一致)
alpha      = 2.0 / (20 + 1)
dailyEMA20 = alpha * close + (1 - alpha) * prevDailyEMA20

// // 固定写法(当天是一条水平线),需要时换成这一行
// dailyEMA20   = request.security(syminfo.tickerid, "D", ta.ema(close, 20)[1], lookahead=barmerge.lookahead_on)

// 上一个完整交易日 Low / High,上一个完整周 Low
prevDayLow   = request.security(syminfo.tickerid, "D", low[1],   lookahead=barmerge.lookahead_on)
prevDayHigh  = request.security(syminfo.tickerid, "D", high[1],  lookahead=barmerge.lookahead_on)
prevWeekLow  = request.security(syminfo.tickerid, "W", low[1],   lookahead=barmerge.lookahead_on)
prevDayClose = request.security(syminfo.tickerid, "D", close[1], lookahead=barmerge.lookahead_on)
todayOpen    = request.security(syminfo.tickerid, "D", open,     lookahead=barmerge.lookahead_on)

onlyGapDown  = input.bool(false, "只在 Gap Down 当天提醒(今日开盘 < 昨日收盘)")
isGapDownDay = todayOpen < prevDayClose

// =====================================================
// 6. K线收盘从下往上站上(含影线跌破后收回)
// =====================================================
// 在 30m 或 65m 图上生效,按当前图表周期的K线收盘判断
isAlertTf  = timeframe.period == "30" or timeframe.period == "65"
newDay     = timeframe.change("D")
oncePerDay = input.bool(true, "每条线每天最多提醒一次")

gate = isAlertTf and barstate.isconfirmed and (not onlyGapDown or isGapDownDay)

// 之前在线下:本根最低价跌破过线,或者上一根收盘在线下
// 现在在线上:本根收盘 > 线
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

// 三角标记:橙=Daily EMA20,绿=前日低点,蓝=前周低点,红=前日高点
plotshape(crossEMA, title="Close > Daily EMA20",   style=shape.triangleup, location=location.belowbar, color=color.orange, size=size.tiny)
plotshape(crossPDL, title="Close > Prev Day Low",  style=shape.triangleup, location=location.belowbar, color=color.green,  size=size.tiny)
plotshape(crossPWL, title="Close > Prev Week Low", style=shape.triangleup, location=location.belowbar, color=color.blue,   size=size.tiny)
plotshape(crossPDH, title="Close > Prev Day High", style=shape.triangleup, location=location.belowbar, color=color.red,    size=size.tiny)

// =====================================================
// 6b. 价位线:前日 Low / 前日 High / 前周 Low(延伸到右侧 + 文字 + 价格)
// =====================================================
showLevels = input.bool(true, "显示价位线(Day Low / Day High / Week Low)")

var int dayStartBar = na
if newDay
    dayStartBar := bar_index

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

    drawLevel(prevDayLow,  "Day Low",  color.black)
    drawLevel(prevDayHigh, "Day High", color.black)
    drawLevel(prevWeekLow, "Week Low", color.black)

// =====================================================
// 7. Alerts(K线收盘确认)
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
// 7b. 实时提醒:价格盘中从下往上穿过 Prev Day Low / Prev Week Low / Prev Day High(不等收盘)
//     单独建 4)/5)/7) 时选 Once Per Bar;用 Any alert() function call 时不用设置
// =====================================================
liveGate = timeframe.isintraday and (not onlyGapDown or isGapDownDay)

// 先在每根K线上都计算 crossover,再和 gate 组合
rawLivePDL = ta.crossover(close, prevDayLow)
rawLivePWL = ta.crossover(close, prevWeekLow)
rawLivePDH = ta.crossover(close, prevDayHigh)

liveCrossPDL = liveGate and rawLivePDL
liveCrossPWL = liveGate and rawLivePWL
liveCrossPDH = liveGate and rawLivePDH

alertcondition(liveCrossPDL, title="4) LIVE Price > Prev Day Low",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Day Low (live)")
alertcondition(liveCrossPWL, title="5) LIVE Price > Prev Week Low",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Week Low (live)")
alertcondition(liveCrossPDH, title="7) LIVE Price > Prev Day High",
     message="{{ticker}} | Price {{close}} crossed ABOVE Previous Day High (live)")

if liveCrossPDL or liveCrossPWL or liveCrossPDH
    string liveMsg = syminfo.ticker + " | LIVE " + str.tostring(close) + " crossed ABOVE: "
    liveMsg += liveCrossPDL ? "Previous Day Low " : ""
    liveMsg += liveCrossPWL ? "Previous Week Low " : ""
    liveMsg += liveCrossPDH ? "Previous Day High " : ""
    // alert(liveMsg, alert.freq_once_per_bar)
    alert(liveMsg, alert.freq_all)
```
