import io,json,os,re,zipfile
from datetime import datetime
from pathlib import Path
import requests,pandas as pd

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/company.json").read_text(encoding="utf-8"))
KEY=os.getenv("DART_API_KEY")
if not KEY: raise SystemExit("DART_API_KEY secret is required.")
BASE="https://opendart.fss.or.kr/api"
REPORTS=CFG["reports"]
ALIASES={
"revenue":["매출액","수익(매출액)","영업수익"],
"operating_profit":["영업이익","영업이익(손실)"],
"net_income":["당기순이익","당기순이익(손실)"],
"assets":["자산총계"],"liabilities":["부채총계"],"equity":["자본총계"],
"current_assets":["유동자산"],"current_liabilities":["유동부채"],
"cash":["현금및현금성자산"],"ocf":["영업활동현금흐름","영업활동으로 인한 현금흐름"],
"eps":["기본주당이익","기본주당순이익"]}

def api(path,params):
    r=requests.get(f"{BASE}/{path}",params={"crtfc_key":KEY,**params},timeout=90)
    r.raise_for_status()
    return r.json()

def number(x):
    try:
        s=str(x).replace(",","").strip()
        return None if s in ("","-") else float(s)
    except: return None

def modern(year,code):
    d=api("fnlttSinglAcntAll.json",{"corp_code":CFG["corp_code"],"bsns_year":str(year),"reprt_code":code,"fs_div":CFG["fs_div"]})
    if d.get("status")!="000": return []
    return [{"year":year,"report_code":code,"account_nm":x.get("account_nm"),"amount":number(x.get("thstrm_amount"))} for x in d.get("list",[])]

def legacy(year,code):
    d=api("list.json",{"corp_code":CFG["corp_code"],"bgn_de":f"{year}0101","end_de":f"{year+1}1231","page_count":"100"})
    if d.get("status")!="000": return []
    names={REPORTS["annual"]:"사업보고서",REPORTS["half"]:"반기보고서",REPORTS["q1"]:"분기보고서",REPORTS["q3"]:"분기보고서"}
    fs=[x for x in d.get("list",[]) if names[code] in x.get("report_nm","")]
    if code==REPORTS["q1"]: fs=[x for x in fs if "1분기" in x.get("report_nm","")]
    if code==REPORTS["q3"]: fs=[x for x in fs if "3분기" in x.get("report_nm","")]
    if not fs:return []
    f=sorted(fs,key=lambda x:x.get("rcept_dt",""))[-1]
    r=requests.get(f"{BASE}/document.xml",params={"crtfc_key":KEY,"rcept_no":f["rcept_no"]},timeout=120)
    r.raise_for_status()
    try:z=zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:return []
    out=[]
    for n in z.namelist():
        if not n.lower().endswith((".xml",".html",".htm")):continue
        t=re.sub(r"<[^>]+>"," ",z.read(n).decode("utf-8","ignore"))
        t=re.sub(r"\s+"," ",t)
        for key,als in ALIASES.items():
            for a in als:
                m=re.search(re.escape(a)+r".{0,300}",t)
                if not m:continue
                vals=re.findall(r"-?\d{1,3}(?:,\d{3})+|-?\d+(?:\.\d+)?",m.group(0))
                if vals: out.append({"year":year,"report_code":code,"account_nm":key,"amount":number(vals[0])})
                break
    return out

def normalize(rows):
    d={}
    for r in rows:
        a=r.get("account_nm","")
        for k,als in ALIASES.items():
            if a in als or any(v in a for v in als):
                if r.get("amount") is not None:d[k]=r["amount"]
    return d

def pct(a,b): return None if a is None or b in (None,0) else a/b*100

def main():
    raw=[]
    for y in range(CFG["start_year"],datetime.now().year+1):
        for c in REPORTS.values():
            raw += modern(y,c) if y>=2015 else legacy(y,c)
    labels={REPORTS["annual"]:"annual",REPORTS["half"]:"half",REPORTS["q1"]:"quarterly",REPORTS["q3"]:"quarterly"}
    data=[]
    for y in range(CFG["start_year"],datetime.now().year+1):
        for c in REPORTS.values():
            d=normalize([r for r in raw if r["year"]==y and r["report_code"]==c])
            if not d: continue
            d.update({"year":y,"report":labels[c],"report_code":c})
            d.update({"operating_margin":pct(d.get("operating_profit"),d.get("revenue")),"net_margin":pct(d.get("net_income"),d.get("revenue")),"debt_ratio":pct(d.get("liabilities"),d.get("equity")),"equity_ratio":pct(d.get("equity"),d.get("assets")),"current_ratio":pct(d.get("current_assets"),d.get("current_liabilities")),"roa":pct(d.get("net_income"),d.get("assets")),"roe":pct(d.get("net_income"),d.get("equity")),"ocf_ratio":pct(d.get("ocf"),d.get("revenue"))})
            data.append(d)
    data.sort(key=lambda x:(x["year"],x["report"]))
    (ROOT/"data").mkdir(exist_ok=True);(ROOT/"dashboard").mkdir(exist_ok=True)
    pd.DataFrame(data).to_csv(ROOT/"data/financials.csv",index=False,encoding="utf-8-sig")
    payload={"company":CFG["company"],"updated_at":datetime.now().isoformat(),"annual":[],"half":[],"quarterly":[]}
    for x in data: payload[x["report"]].append(x)
    text=json.dumps(payload,ensure_ascii=False,indent=2)
    (ROOT/"data/dashboard.json").write_text(text,encoding="utf-8")
    (ROOT/"dashboard/data.json").write_text(text,encoding="utf-8")

if __name__=="__main__":main()
