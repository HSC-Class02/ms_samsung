import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/company.json").read_text(encoding="utf-8"))
KEY = os.getenv("DART_API_KEY")
if not KEY:
    raise SystemExit("DART_API_KEY secret is required.")

BASE = "https://opendart.fss.or.kr/api"
REPORTS = CFG["reports"]
ALIASES = {
    "revenue": ["매출액", "수익(매출액)", "영업수익"],
    "operating_profit": ["영업이익", "영업이익(손실)"],
    "net_income": ["당기순이익", "당기순이익(손실)", "분기순이익"],
    "assets": ["자산총계"],
    "liabilities": ["부채총계"],
    "equity": ["자본총계"],
    "current_assets": ["유동자산"],
    "current_liabilities": ["유동부채"],
    "cash": ["현금및현금성자산"],
    "ocf": ["영업활동현금흐름", "영업활동으로 인한 현금흐름"],
    "eps": ["기본주당이익", "기본주당순이익", "기본주당이익(손실)"],
}
STATEMENTS = {
    "assets": {"BS"}, "liabilities": {"BS"}, "equity": {"BS"},
    "current_assets": {"BS"}, "current_liabilities": {"BS"}, "cash": {"BS"},
    "revenue": {"IS", "CIS"}, "operating_profit": {"IS", "CIS"},
    "net_income": {"IS", "CIS"}, "eps": {"IS", "CIS"}, "ocf": {"CF"},
}
ACCOUNT_IDS = {
    "revenue": {"ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"},
    "operating_profit": {"dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"},
    "net_income": {"ifrs-full_ProfitLoss", "ifrs-full_ProfitLossAttributableToOwnersOfParent"},
    "assets": {"ifrs-full_Assets"},
    "liabilities": {"ifrs-full_Liabilities"},
    "equity": {"ifrs-full_Equity"},
    "current_assets": {"ifrs-full_CurrentAssets"},
    "current_liabilities": {"ifrs-full_CurrentLiabilities"},
    "cash": {"ifrs-full_CashAndCashEquivalents"},
    "ocf": {"ifrs-full_CashFlowsFromUsedInOperatingActivities"},
    "eps": {"ifrs-full_BasicEarningsLossPerShare"},
}

def api(path, params):
    r = requests.get(f"{BASE}/{path}", params={"crtfc_key": KEY, **params}, timeout=90)
    r.raise_for_status()
    return r.json()

def number(value):
    try:
        s = str(value).replace(",", "").strip()
        return None if s in ("", "-", "None") else float(s)
    except (TypeError, ValueError):
        return None

def modern(year, code, corp_code=None):
    response = api("fnlttSinglAcntAll.json", {
        "corp_code": corp_code or CFG["corp_code"], "bsns_year": str(year),
        "reprt_code": code, "fs_div": CFG["fs_div"],
    })
    if response.get("status") == "013":
        return []
    if response.get("status") != "000":
        raise RuntimeError(f"DART {year}/{code}: {response.get('status')} {response.get('message')}")
    rows = []
    for row in response.get("list", []):
        rows.append({
            "year": year, "report_code": code,
            "account_id": row.get("account_id", ""),
            "account_nm": row.get("account_nm", ""),
            "sj_div": row.get("sj_div", ""),
            "fs_div": row.get("fs_div", ""),
            "currency": row.get("currency", ""),
            "amount": number(row.get("thstrm_amount")),
            "cumulative_amount": number(row.get("thstrm_add_amount")),
            "raw": row,
        })
    return rows

def legacy(year, code):
    # Historical XML layout differs by filing; keep these values clearly tagged for validation.
    response = api("list.json", {
        "corp_code": CFG["corp_code"], "bgn_de": f"{year}0101",
        "end_de": f"{year + 1}1231", "page_count": "100",
    })
    if response.get("status") != "000":
        return []
    names = {REPORTS["annual"]: "사업보고서", REPORTS["half"]: "반기보고서",
             REPORTS["q1"]: "분기보고서", REPORTS["q3"]: "분기보고서"}
    filings = [x for x in response.get("list", []) if names[code] in x.get("report_nm", "")]
    if code == REPORTS["q1"]:
        filings = [x for x in filings if "1분기" in x.get("report_nm", "")]
    elif code == REPORTS["q3"]:
        filings = [x for x in filings if "3분기" in x.get("report_nm", "")]
    elif code == REPORTS["half"]:
        filings = [x for x in filings if "반기" in x.get("report_nm", "")]
    else:
        filings = [x for x in filings if "사업보고서" in x.get("report_nm", "")]
    if not filings:
        return []
    filing = sorted(filings, key=lambda x: x.get("rcept_dt", ""))[-1]
    result = requests.get(f"{BASE}/document.xml", params={
        "crtfc_key": KEY, "rcept_no": filing["rcept_no"]
    }, timeout=120)
    result.raise_for_status()
    try:
        archive = zipfile.ZipFile(io.BytesIO(result.content))
    except zipfile.BadZipFile:
        return []
    out = []
    for name in archive.namelist():
        if not name.lower().endswith((".xml", ".html", ".htm")):
            continue
        text = re.sub(r"<[^>]+>", " ", archive.read(name).decode("utf-8", "ignore"))
        text = re.sub(r"\s+", " ", text)
        for metric, aliases in ALIASES.items():
            for alias in aliases:
                match = re.search(re.escape(alias) + r".{0,180}", text)
                if not match:
                    continue
                values = re.findall(r"-?\d{1,3}(?:,\d{3})+|-?\d+(?:\.\d+)?", match.group(0))
                if values:
                    out.append({"year": year, "report_code": code, "account_nm": alias,
                                "account_id": "", "sj_div": "", "amount": number(values[0]),
                                "cumulative_amount": None, "legacy_unverified": True})
                break
    return out

def matches(row, metric):
    # Never use substring matching: it can confuse total equity with share capital, etc.
    if row.get("sj_div") and row["sj_div"] not in STATEMENTS[metric]:
        return False
    account_id = row.get("account_id", "")
    if account_id and account_id in ACCOUNT_IDS.get(metric, set()):
        return True
    name = row.get("account_nm", "").strip()
    return name in ALIASES[metric]

def choose(rows, metric, code):
    candidates = [r for r in rows if matches(r, metric) and (
        r.get("amount") is not None or
        (code == REPORTS["half"] and r.get("cumulative_amount") is not None
         and metric in {"revenue", "operating_profit", "net_income", "eps"})
    )]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (
        0 if r.get("account_id") in ACCOUNT_IDS.get(metric, set()) else 1,
        0 if r.get("sj_div") == ("BS" if metric in {"assets","liabilities","equity","current_assets","current_liabilities","cash"} else "CF" if metric == "ocf" else "CIS") else 1,
    ))
    row = candidates[0]
    # Half-year income-statement lines should use the six-month cumulative amount.
    if code == REPORTS["half"] and metric in {"revenue", "operating_profit", "net_income", "eps"}:
        return row.get("cumulative_amount") if row.get("cumulative_amount") is not None else row["amount"]
    return row["amount"]

def pct(numerator, denominator):
    return None if numerator is None or denominator in (None, 0) else numerator / denominator * 100

def normalize(rows, year, code):
    out = {}
    for metric in ALIASES:
        out[metric] = choose(rows, metric, code)
    out.update({"year": year, "report_code": code})
    out["report"] = {REPORTS["annual"]: "annual", REPORTS["half"]: "half",
                     REPORTS["q1"]: "quarterly", REPORTS["q3"]: "quarterly"}[code]
    out.update({
        "operating_margin": pct(out["operating_profit"], out["revenue"]),
        "net_margin": pct(out["net_income"], out["revenue"]),
        "debt_ratio": pct(out["liabilities"], out["equity"]),
        "equity_ratio": pct(out["equity"], out["assets"]),
        "current_ratio": pct(out["current_assets"], out["current_liabilities"]),
        "roa": pct(out["net_income"], out["assets"]),
        "roe": pct(out["net_income"], out["equity"]),
        "ocf_ratio": pct(out["ocf"], out["revenue"]),
    })
    return out

def validation_issues(row):
    issues = []
    required = ["revenue", "operating_profit", "net_income", "assets", "liabilities", "equity"]
    missing = [key for key in required if row.get(key) is None]
    if missing:
        issues.append("필수 항목 누락: " + ", ".join(missing))
    assets, liabilities, equity = row.get("assets"), row.get("liabilities"), row.get("equity")
    if assets is not None and liabilities is not None and equity is not None:
        if assets <= 0 or liabilities < 0 or equity <= 0:
            issues.append("자산/부채/자본 부호 확인")
        elif abs(assets - liabilities - equity) / assets > 0.03:
            issues.append("자산총계와 부채+자본 불일치(3% 초과)")
    if row.get("current_assets") is not None and assets and row["current_assets"] > assets * 1.03:
        issues.append("유동자산이 자산총계 초과")
    if row.get("current_liabilities") is not None and liabilities and row["current_liabilities"] > liabilities * 1.03:
        issues.append("유동부채가 부채총계 초과")
    if row.get("revenue") is not None and row["revenue"] <= 0:
        issues.append("매출액 0 이하")
    if row.get("net_income") is not None and row.get("operating_profit") is not None:
        if abs(row["net_income"]) > max(abs(row["revenue"] or 0) * 2, 1):
            issues.append("순이익 규모 확인")
    return issues

def won_trillion(value):
    return "자료 없음" if value is None else f"{value / 1_000_000_000_000:,.1f}조 원"

def pct_text(value):
    return "자료 없음" if value is None else f"{value:.1f}%"

def make_report(annual, invalid, peers=None):
    peers = peers or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    valid = [r for r in annual if not validation_issues(r)]
    lines = [
        "# 삼성전자 재무분석 보고서",
        "",
        f"- 분석 대상: 삼성전자(005930), 연결재무제표(CFS)",
        f"- 데이터 갱신 시각: {now}",
        "- 자료 출처: 금융감독원 OpenDART 정기보고서",
        "- 단위: 금액은 조 원, 비율은 %",
        "",
        "## 1. 분석 목적 및 방법",
        "OpenDART 전체 재무제표 API를 이용해 2015년 이후 정기보고서의 주요 재무 항목을 수집하고, 수익성·안정성·유동성 지표를 계산한다. 2010~2014년은 자동 추출값의 신뢰성을 충분히 확보하지 못해 확정 분석 범위에서 제외했다. 표준계정 ID와 재무제표 구분을 우선해 항목을 매핑한다.",
        "",
        "## 2. 데이터 품질 점검",
        f"- 수집된 연간 행: {len(annual)}개",
        f"- 회계 등식 및 기본 범위 검증 통과: {len(valid)}개",
        f"- 검증 실패: {len(invalid)}개",
        "- 주요 검증: 자산총계 ≈ 부채총계 + 자본총계, 유동자산 ≤ 자산총계, 유동부채 ≤ 부채총계, 매출액·순이익 규모 확인",
        "",
    ]
    if invalid:
        lines += ["**주의: 아래 검증 실패 항목은 분석 결론과 대시보드에 사용하지 않는다.**", ""]
        for row, issues in invalid:
            lines.append(f"- {row.get('year')}년: {', '.join(issues)}")
        lines.append("")
    if len(valid) < 3:
        lines += [
            "## 3. 분석 결론",
            "현재 검증을 통과한 연간 데이터가 충분하지 않아 실적 추세에 대한 결론을 보류한다. OpenDART 원문과 계정 매핑을 확인한 뒤 워크플로우를 다시 실행해야 한다. 검증 실패 수치를 이용해 성장률이나 재무비율을 해석하면 잘못된 결론이 발생할 수 있다.",
            "",
            "## 4. 한계 및 후속 작업",
            "- 2010~2014년 원문 XML은 서식 차이가 있어 자동 추출값을 원문과 대조해야 한다.",
            "- 동종기업 표는 현재 비교 후보 목록이며, 실제 동종기업 재무비교 데이터가 수집된 것은 아니다.",
            "- 반기·분기 손익 수치는 보고기간의 누적/당기 구분을 확인하고 비교해야 한다.",
        ]
        return "\n".join(lines) + "\n"
    first, last = valid[0], valid[-1]
    lines += [
        "## 3. 주요 실적 추이",
        f"- {first['year']}년 매출액은 {won_trillion(first.get('revenue'))}, {last['year']}년 매출액은 {won_trillion(last.get('revenue'))}이다.",
        f"- {last['year']}년 영업이익은 {won_trillion(last.get('operating_profit'))}, 당기순이익은 {won_trillion(last.get('net_income'))}이다.",
        f"- {last['year']}년 영업이익률은 {pct_text(last.get('operating_margin'))}, 순이익률은 {pct_text(last.get('net_margin'))}이다.",
        "",
        "## 4. 최근 연간 지표",
        "| 사업연도 | 매출액 | 영업이익 | 당기순이익 | 영업이익률 | 부채비율 | 유동비율 | ROE |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in valid[-5:]:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["year"], won_trillion(row.get("revenue")), won_trillion(row.get("operating_profit")),
            won_trillion(row.get("net_income")), pct_text(row.get("operating_margin")),
            pct_text(row.get("debt_ratio")), pct_text(row.get("current_ratio")), pct_text(row.get("roe"))))
    lines += [
        "",
        "## 5. 실적 해석",
        "- 2023년에는 반도체 업황 부진 등의 영향과 함께 영업이익률이 낮아졌으며, 2024~2025년에는 매출과 영업이익이 회복되는 흐름을 보였다.",
        f"{last['year']}년 매출액과 영업이익은 전년 대비 각각 {((last['revenue'] / valid[-2]['revenue'] - 1) * 100):.1f}%, {((last['operating_profit'] / valid[-2]['operating_profit'] - 1) * 100):.1f}% 변화했다." if len(valid) >= 2 and valid[-2].get("revenue") and valid[-2].get("operating_profit") else "전년 대비 성장률은 비교 가능한 연간 데이터가 부족해 계산하지 않았다.",
        f"{last['year']}년 부채비율은 {pct_text(last.get('debt_ratio'))}이고 유동비율은 {pct_text(last.get('current_ratio'))}로, 단기 지급능력과 재무구조를 함께 볼 필요가 있다.",
        "- 비율 변화만으로 원인을 단정할 수 없으므로 반도체 업황, 제품 믹스, 환율, 투자 및 현금흐름을 함께 검토해야 한다.",
        "",
        "## 6. 동종기업 비교",
        "최근 공통 사업연도의 연결재무제표 기준 수치다. 기업별 회계정책과 사업구조가 달라 단순 순위보다는 규모·마진·부채비율을 함께 해석한다.",
        "| 기업 | 사업연도 | 매출액 | 영업이익 | 당기순이익 | 영업이익률 | 부채비율 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    peer_valid = [p for p in peers if not p.get("issues") and p.get("revenue") is not None]
    for p in peer_valid:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            p["name"], p["year"], won_trillion(p.get("revenue")), won_trillion(p.get("operating_profit")),
            won_trillion(p.get("net_income")), pct_text(p.get("operating_margin")), pct_text(p.get("debt_ratio"))))
    if not peer_valid:
        lines.append("| 동종기업 | - | 데이터 미수집 | - | - | - | - |")
    lines += [
        "",
        "## 7. 지표 정의 및 한계",
        "- 부채비율 = 부채총계 ÷ 자본총계 × 100, 유동비율 = 유동자산 ÷ 유동부채 × 100.",
        "- ROA·ROE는 연평균 자산·자본을 쓰는 것이 더 적절하지만, 본 대시보드의 단순 계산은 기말잔액 기준 근사치다.",
        "- 과거 2010~2014년은 원문 XML 서식 차이로 자동 추출의 정확성을 보장할 수 없어 확정 비교에서 제외했다.",
        "- 본 보고서는 공시자료 기반 학습용 기초 분석이며 투자 권유가 아니다.",
    ]
    return "\n".join(lines) + "\n"

def get_corp_codes():
    response = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": KEY}, timeout=90)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml_name = next((n for n in archive.namelist() if n.lower().endswith(".xml")), None)
        if not xml_name:
            return {}
        from lxml import etree
        root = etree.fromstring(archive.read(xml_name))
        mapping = {}
        for node in root.xpath(".//list"):
            stock = (node.findtext("stock_code") or "").strip()
            corp = (node.findtext("corp_code") or "").strip()
            name = (node.findtext("corp_name") or "").strip()
            if stock and corp:
                mapping[stock] = {"corp_code": corp, "name": name}
        return mapping

def collect_peers():
    peers_cfg = CFG.get("peers", [])
    if not peers_cfg:
        return []
    corp_map = get_corp_codes()
    year_now = datetime.now().year
    start = max(2021, year_now - 5)
    out = []
    for peer in peers_cfg:
        corp = corp_map.get(peer["stock_code"])
        if not corp:
            out.append({"name": peer["name"], "stock_code": peer["stock_code"], "issues": ["DART 기업코드 조회 실패"]})
            continue
        candidates = []
        for year in range(start, year_now + 1):
            try:
                rows = modern(year, REPORTS["annual"], corp["corp_code"])
                if rows:
                    row = normalize(rows, year, REPORTS["annual"])
                    issues = validation_issues(row)
                    if not issues:
                        candidates.append(row)
            except Exception as exc:
                print(f"Peer data warning: {peer['name']} {year}: {exc}")
        if candidates:
            row = candidates[-1]
            row.update({"name": peer["name"], "stock_code": peer["stock_code"], "issues": []})
            out.append(row)
        else:
            out.append({"name": peer["name"], "stock_code": peer["stock_code"], "issues": ["검증을 통과한 연간 데이터 없음"]})
    return out

def main():
    raw = []
    for year in range(max(2015, CFG["start_year"]), datetime.now().year + 1):
        for code in REPORTS.values():
            raw.extend(modern(year, code))

    data = []
    for year in range(max(2015, CFG["start_year"]), datetime.now().year + 1):
        for code in REPORTS.values():
            subset = [r for r in raw if r.get("year") == year and r.get("report_code") == code]
            if not subset:
                continue
            row = normalize(subset, year, code)
            # Exclude rows with no usable headline values.
            if row.get("revenue") is None and row.get("assets") is None:
                continue
            data.append(row)

    data.sort(key=lambda r: (r["year"], r["report_code"]))
    annual = [r for r in data if r["report"] == "annual"]
    invalid = [(r, validation_issues(r)) for r in annual if validation_issues(r)]
    valid_data = [r for r in data if not validation_issues(r)]
    # Do not publish obviously inconsistent rows; record them in the report for auditability.
    payload = {"company": CFG["company"], "updated_at": datetime.now().isoformat(),
               "annual": [], "half": [], "quarterly": []}
    for row in valid_data:
        payload[row["report"]].append(row)

    peers = collect_peers()
    payload["peers"] = peers

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "dashboard").mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    pd.DataFrame(data).to_csv(ROOT / "data/financials.csv", index=False, encoding="utf-8-sig")
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    (ROOT / "data/dashboard.json").write_text(json_text, encoding="utf-8")
    (ROOT / "dashboard/data.json").write_text(json_text, encoding="utf-8")
    (ROOT / "reports/financial_analysis.md").write_text(make_report(annual, invalid, peers), encoding="utf-8")

    if len(payload["annual"]) < 3:
        raise SystemExit(
            f"Data validation blocked dashboard publication: only {len(payload['annual'])} annual rows passed. "
            "Review reports/financial_analysis.md and DART account mappings before publishing."
        )

if __name__ == "__main__":
    main()
