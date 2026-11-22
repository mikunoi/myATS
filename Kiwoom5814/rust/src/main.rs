//! Offline strategy worker. No credentials, DB writes or broker order transport.
use bigdecimal::{BigDecimal as D, Context, RoundingMode};
use num_traits::{ToPrimitive, Zero};
use serde_json::{json, Value as V};
use std::{
    io::{self, BufRead, Write},
    num::NonZeroU64,
    str::FromStr,
};

fn ctx() -> Context {
    Context::new(NonZeroU64::new(28).unwrap(), RoundingMode::HalfEven)
}
fn rounded(v: D) -> D {
    ctx().round_decimal(v)
}
fn add(a: &D, b: &D) -> D {
    rounded(a + b)
}
fn sub(a: &D, b: &D) -> D {
    rounded(a - b)
}
fn mul(a: &D, b: &D) -> D {
    rounded(a * b)
}
fn div(a: &D, b: &D) -> Result<D, String> {
    if b.is_zero() {
        return Err("zero divisor".into());
    }
    Ok(rounded(a / b))
}
fn decimal(v: &V) -> Result<D, String> {
    let text = v.as_str().ok_or("decimal must be a string")?;
    if text.len() > 100
        || text.contains(['e', 'E'])
            && text
                .split(['e', 'E'])
                .last()
                .unwrap_or("")
                .parse::<i64>()
                .map_or(true, |e| e.unsigned_abs() > 100)
    {
        return Err("decimal outside supported range".into());
    }
    D::from_str(text).map_err(|_| "invalid decimal".into())
}
fn d(v: &V, key: &str) -> Result<D, String> {
    decimal(&v[key])
}
fn opt(v: &V, key: &str) -> Result<Option<D>, String> {
    if v[key].is_null() {
        Ok(None)
    } else {
        Ok(Some(d(v, key)?))
    }
}
fn int(v: &V, key: &str) -> Result<u64, String> {
    v[key].as_u64().ok_or(format!("invalid integer: {key}"))
}
fn ds(d: Option<D>) -> V {
    d.map(|d| json!(d.to_string())).unwrap_or(V::Null)
}
fn ready(p: &V, c: &V) -> Result<Option<(D, D, D, D)>, String> {
    Ok(
        match (
            opt(p, "tenkan")?,
            opt(p, "kijun")?,
            opt(c, "tenkan")?,
            opt(c, "kijun")?,
        ) {
            (Some(pt), Some(pk), Some(ct), Some(ck)) => Some((pt, pk, ct, ck)),
            _ => None,
        },
    )
}
fn common(c: &V) -> Result<bool, String> {
    Ok(
        matches!((opt(c,"tenkan")?, opt(c,"kijun")?), (Some(t), Some(k)) if t>=k)
            && c["chikou_valid"] == true,
    )
}
fn indicators(v: &V) -> Result<V, String> {
    let periods = v["periods"].as_array().ok_or("periods required")?;
    if periods.len() != 3 {
        return Err("three periods required".into());
    }
    let per: Vec<usize> = periods
        .iter()
        .map(|v| {
            v.as_u64()
                .filter(|p| *p > 0 && *p <= 10000)
                .map(|p| p as usize)
                .ok_or("invalid period".to_string())
        })
        .collect::<Result<_, _>>()?;
    let n = int(v, "atr_period")? as usize;
    if n == 0 || n > 10000 {
        return Err("invalid ATR period".into());
    }
    let mut candles = v["candles"].as_array().ok_or("candles required")?.clone();
    if candles.len() > 100000 {
        return Err("too many candles".into());
    }
    if candles.iter().any(|c| !c["date"].is_string()) {
        return Err("invalid candle date".into());
    }
    candles.sort_by(|a, b| a["date"].as_str().cmp(&b["date"].as_str()));
    let mut prices = Vec::new();
    for (i, c) in candles.iter().enumerate() {
        if i > 0 && c["date"] == candles[i - 1]["date"] {
            return Err("duplicate candle date".into());
        }
        let (o, h, l, cl) = (d(c, "open")?, d(c, "high")?, d(c, "low")?, d(c, "close")?);
        if h < o || h < cl || h < l || l > o || l > cl {
            return Err("inconsistent OHLC".into());
        }
        prices.push((h, l, cl));
    }
    let mut ranges = Vec::new();
    let mut atr: Option<D> = None;
    let mut result = Vec::new();
    for (i, (h, l, c)) in prices.iter().enumerate() {
        let mut tr = sub(h, l);
        if i > 0 {
            tr = tr
                .max(sub(h, &prices[i - 1].2).abs())
                .max(sub(l, &prices[i - 1].2).abs());
        }
        ranges.push(tr.clone());
        if i == n - 1 {
            let sum = ranges.iter().fold(D::zero(), |s, x| add(&s, x));
            atr = Some(div(&sum, &D::from(n as u64))?);
        } else if i >= n {
            atr = Some(div(
                &add(&mul(atr.as_ref().unwrap(), &D::from((n - 1) as u64)), &tr),
                &D::from(n as u64),
            )?);
        }
        let mid = |period: usize| -> Option<D> {
            if i + 1 < period {
                return None;
            }
            let window = &prices[i + 1 - period..=i];
            let hi = window.iter().map(|x| &x.0).max().unwrap();
            let lo = window.iter().map(|x| &x.1).min().unwrap();
            Some(div(&add(hi, lo), &D::from(2)).unwrap())
        };
        let tenkan = mid(per[0]);
        let kijun = mid(per[1]);
        let a = match (&tenkan, &kijun) {
            (Some(t), Some(k)) => Some(div(&add(t, k), &D::from(2))?),
            _ => None,
        };
        result.push(json!({"date":candles[i]["date"],"close":c.to_string(),"tenkan":ds(tenkan),"kijun":ds(kijun),
            "senkou_a":ds(a),"senkou_b":ds(mid(per[2])),"atr":ds(atr.clone()),
            "chikou_valid":if i>=per[1] {json!(c>=&prices[i-per[1]].2)} else {V::Null}}));
    }
    Ok(json!(result))
}
fn execute(v: &V) -> Result<V, String> {
    let op = v["op"].as_str().ok_or("operation required")?;
    let p = &v["previous"];
    let c = &v["current"];
    match op {
        "hello" => Ok(
            json!({"protocol":1,"engine":"rust","decimal_precision":28,"broker_transport":false}),
        ),
        "indicators" => indicators(v),
        "quantity" => {
            let capital = d(v, "capital")?;
            let atr = d(v, "atr")?;
            let risk = d(v, "risk")?;
            let multiple = d(v, "multiple")?;
            if capital < D::zero()
                || atr <= D::zero()
                || risk <= D::zero()
                || risk > D::from(1)
                || multiple <= D::zero()
            {
                return Err("invalid sizing input".into());
            }
            let q = div(&mul(&capital, &risk), &mul(&multiple, &atr))?;
            Ok(json!(q.to_u64().ok_or("quantity outside range")?))
        }
        "entry_common" | "initial_entry" | "cycle_reentry" => Ok(json!(common(c)?)),
        "rule3_reentry" => Ok(json!(
            common(c)?
                && opt(c, "kijun")?.is_some_and(|k| d(v, "current_price").is_ok_and(|p| p >= k))
        )),
        "weekly_rule3_reentry" => {
            let eligible = common(c)?;
            let prices = (d(v, "previous_price")?, d(v, "current_price")?);
            Ok(json!(
                eligible
                    && matches!((opt(p,"kijun")?,opt(c,"kijun")?), (Some(pk),Some(ck)) if prices.0<pk && prices.1>=ck)
            ))
        }
        "crossed_above" => Ok(json!(
            ready(p, c)?.is_some_and(|(pt, pk, ct, ck)| pt <= pk && ct > ck)
        )),
        "crossed_below" => Ok(json!(
            ready(p, c)?.is_some_and(|(pt, pk, ct, ck)| pt >= pk && ct < ck)
        )),
        "missed_daily_cross_exit" => {
            let price = d(v, "current_price")?;
            Ok(json!(ready(p, c)?.is_some_and(|(pt, pk, ct, ck)| pt < pk
                && ct < ck
                && price < ck)))
        }
        "addon_entry" => {
            if int(v, "addon_count")? >= int(v, "cycle_limit")?
                || int(v, "addons_today")? >= int(v, "daily_limit")?
            {
                return Ok(json!(false));
            }
            let atr = d(v, "latest_entry_atr")?;
            let multiple = d(v, "multiple")?;
            if atr <= D::zero() || multiple <= D::zero() {
                return Err("invalid add-on ATR".into());
            }
            Ok(json!(
                d(v, "current_price")? >= add(&d(v, "latest_entry_price")?, &mul(&atr, &multiple))
            ))
        }
        "sell_signals" => {
            let price = d(v, "current_price")?;
            let mode = v["mode"].as_str().ok_or("mode required")?;
            let mut reasons = Vec::new();
            if mode == "WEEKLY" {
                if opt(c, "kijun")?.is_some_and(|k| price < k) {
                    reasons.push("SELL_WEEKLY_KIJUN");
                }
            } else if mode == "DAILY" {
                if let (Some(k), Some(atr), Some(entry), Some(entry_atr)) = (
                    opt(c, "kijun")?,
                    opt(c, "atr")?,
                    opt(v, "latest_entry_price")?,
                    opt(v, "latest_entry_atr")?,
                ) {
                    if entry_atr > D::zero() {
                        if ready(p, c)?.is_some_and(|(pt, pk, ct, ck)| pt >= pk && ct < ck) {
                            reasons.push("SELL_RULE_1_BEARISH_CROSS");
                        }
                        if price < sub(&entry, &mul(&D::from(3), &entry_atr)) {
                            reasons.push("SELL_RULE_2_LATEST_ENTRY_3ATR");
                        }
                        if price < sub(&k, &atr) {
                            reasons.push("SELL_RULE_3_KIJUN_CURRENT_ATR");
                        }
                    }
                }
            } else {
                return Err("invalid mode".into());
            }
            Ok(json!(reasons))
        }
        _ => Err("unsupported operation".into()),
    }
}
fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) != Some("worker") {
        println!("Kiwoom5814 Rust strategy core. Usage: kiwoom5814-core worker (JSON lines on stdin). No broker orders.");
        return;
    }
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let mut out = io::stdout().lock();
    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Err(_) => break,
            _ => {}
        }
        let response = if line.len() > 16 * 1024 * 1024 {
            Err("request too large".into())
        } else {
            serde_json::from_str::<V>(&line)
                .map_err(|_| "invalid JSON".into())
                .and_then(|v| execute(&v))
        };
        let value = match response {
            Ok(v) => json!({"ok":true,"result":v}),
            Err(e) => json!({"ok":false,"error":e}),
        };
        if writeln!(out, "{}", value)
            .and_then(|_| out.flush())
            .is_err()
        {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn size_floors() {
        assert_eq!(execute(&json!({"op":"quantity","capital":"100000000","atr":"3000","risk":"0.005","multiple":"3"})).unwrap(),json!(55));
    }
    #[test]
    fn equality_is_entry() {
        assert_eq!(execute(&json!({"op":"entry_common","current":{"tenkan":"10","kijun":"10","chikou_valid":true}})).unwrap(),true);
    }
    #[test]
    fn weekly_equal_is_not_exit() {
        assert_eq!(execute(&json!({"op":"sell_signals","mode":"WEEKLY","current_price":"10","current":{"kijun":"10"}})).unwrap(),json!([]));
    }
    #[test]
    fn invalid_atr_rejected() {
        assert!(execute(
            &json!({"op":"quantity","capital":"1","atr":"0","risk":"0.005","multiple":"3"})
        )
        .is_err());
    }
    #[test]
    fn no_order_api() {
        assert!(execute(&json!({"op":"submit_order"})).is_err());
    }
}
