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

def modern(year, code):
    response = api("fnlttSinglAcntAll.json", {
        "corp_code": CFG["corp_code"], "bsns_year": str(year),
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
    candidates = [r for r in rows if matches(r, metric) and r.get("amount") is not None]
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

def make_report(annual, invalid):
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
        "2010년 이후 공시된 사업보고서·반기보고서·분기보고서에서 주요 재무 항목을 수집하고, 수익성·안정성·유동성 지표를 계산한다. 재무제표의 계정명만 부분 일치시키지 않고 표준계정 ID와 재무제표 구분을 우선해 항목을 매핑한다.",
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
        "## 5. 해석 기준",
        "- 수익성: 매출액, 영업이익, 영업이익률과 순이익률의 변화로 본업 수익성과 최종 수익성을 평가한다.",
        "- 안정성: 부채비율과 자기자본비율을 함께 확인한다. 비율만 단독으로 판단하지 않고 사업구조와 투자 규모를 고려한다.",
        "- 유동성: 유동비율을 활용하되 현금성자산, 재고자산 및 단기부채 구성도 함께 검토한다.",
        "- 효율성: ROA·ROE는 자산·자본의 기간 평균을 쓰는 것이 더 적절하므로, 여기 제시한 기말잔액 기준 값은 근사치다.",
        "",
        "## 6. 한계 및 후속 작업",
        "- 회계연도 간 비교는 연결재무제표 기준으로 통일하고, 재작성·회계정책 변경 여부를 원문에서 확인해야 한다.",
        "- 동종기업 표는 현재 비교 후보 목록이며, 실제 동종기업 재무비교 데이터가 수집된 것은 아니다.",
        "- 본 보고서는 공시자료 기반의 기초 재무분석이며 투자 권유가 아니다.",
    ]
    return "\n".join(lines) + "\n"

def main():
    raw = []
    for year in range(CFG["start_year"], datetime.now().year + 1):
        for code in REPORTS.values():
            raw.extend(modern(year, code) if year >= 2015 else legacy(year, code))

    data = []
    for year in range(CFG["start_year"], datetime.now().year + 1):
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

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "dashboard").mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    pd.DataFrame(data).to_csv(ROOT / "data/financials.csv", index=False, encoding="utf-8-sig")
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    (ROOT / "data/dashboard.json").write_text(json_text, encoding="utf-8")
    (ROOT / "dashboard/data.json").write_text(json_text, encoding="utf-8")
    (ROOT / "reports/financial_analysis.md").write_text(make_report(annual, invalid), encoding="utf-8")

    if len(payload["annual"]) < 3:
        raise SystemExit(
            f"Data validation blocked dashboard publication: only {len(payload['annual'])} annual rows passed. "
            "Review reports/financial_analysis.md and DART account mappings before publishing."
        )

if __name__ == "__main__":
    main()
