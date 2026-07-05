"""fetch_dividends.py / enrich_payout_ratio.py で共有するロジック。"""
import datetime

CURRENT_YEAR = datetime.date.today().year


def annual_totals(dividends):
    """yfinanceのSeries(Date -> 配当額)を年別合計dictに変換。当年は集計中のため除外。"""
    totals = {}
    for ts, amount in dividends.items():
        year = ts.year
        if year >= CURRENT_YEAR:
            continue
        totals[year] = round(totals.get(year, 0) + float(amount), 2)
    return dict(sorted(totals.items()))


def compute_stats(totals):
    years = sorted(totals.keys())
    if not years:
        return {
            "streakIncrease": 0,
            "streakNonDecrease": 0,
            "cagr3": None,
            "cagr5": None,
            "cagr10": None,
        }

    streak_increase = 0
    for i in range(len(years) - 1, 0, -1):
        prev, cur = totals[years[i - 1]], totals[years[i]]
        if cur > prev:
            streak_increase += 1
        else:
            break

    streak_non_decrease = 0
    for i in range(len(years) - 1, 0, -1):
        prev, cur = totals[years[i - 1]], totals[years[i]]
        if cur >= prev:
            streak_non_decrease += 1
        else:
            break

    def cagr(n):
        if len(years) <= n:
            return None
        first, last = totals[years[-1 - n]], totals[years[-1]]
        if first <= 0 or last <= 0:
            return None
        return round(((last / first) ** (1 / n) - 1) * 100, 2)

    return {
        "streakIncrease": streak_increase,
        "streakNonDecrease": streak_non_decrease,
        "cagr3": cagr(3),
        "cagr5": cagr(5),
        "cagr10": cagr(10),
    }


def eps_by_year(ticker):
    """yfinanceの年次決算情報からEPS(潜在株式調整後、無ければ基本的1株益)を年別に取得。
    決算期末日の年を「年」として扱う(暦年の配当集計と厳密には一致しない近似値)。
    """
    try:
        inc = ticker.get_income_stmt(freq="yearly")
    except Exception:
        return {}
    if inc is None or inc.empty:
        return {}

    row_name = None
    for candidate in ("DilutedEPS", "BasicEPS"):
        if candidate in inc.index:
            row_name = candidate
            break
    if row_name is None:
        return {}

    result = {}
    for period_end, value in inc.loc[row_name].items():
        if value is None:
            continue
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue
        if fval != fval:  # NaN
            continue
        result[period_end.year] = fval
    return result


def payout_ratio_by_year(annual, eps_map):
    """年別配当合計とEPSから配当性向(%)を年別に算出。EPSが0以下の年は算出対象外。
    annualはJSON経由で読み込むとキーが文字列になっている場合があるためintに正規化する。
    """
    result = {}
    for year, div in annual.items():
        eps = eps_map.get(int(year))
        if eps is None or eps <= 0:
            continue
        result[int(year)] = round(div / eps * 100, 1)
    return result
