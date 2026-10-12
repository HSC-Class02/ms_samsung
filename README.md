# Samsung Electronics DART Financial Agent

🔗 **[대시보드 바로가기](https://hsc-class02.github.io/ms_samsung/)**

📊 **[재무분석 보고서](reports/financial_analysis.md)**

삼성전자 정기보고서(사업보고서·반기보고서·분기보고서)를 DART OpenDART API로 수집하고 재무지표와 재무비율을 자동 계산하는 프로젝트입니다.

## 주요 기능
- 2010년부터 정기보고서 데이터 구축 시도
- 2015년 이후: OpenDART XBRL 재무제표 API
- 2010~2014년: DART 공시검색 + 원문 XML 보완 수집(서식 차이로 원문 검증 필요)
- 연결재무제표(CFS) 우선
- 연간 / 반기 / 분기 데이터 분리
- 주요 재무지표 및 수익성·안정성·유동성 비율 자동 계산
- 계정 매핑과 회계 등식 기반 데이터 검증
- GitHub Actions 매월 1일 자동 업데이트 및 보고서 생성
- GitHub Pages 정적 대시보드

## 최초 설정
GitHub 저장소 **Settings → Secrets and variables → Actions → New repository secret**
- Name: `DART_API_KEY`
- Value: OpenDART에서 발급한 인증키

API 키는 코드에 직접 넣지 않습니다.

## 자동 업데이트
`.github/workflows/update.yml`이 매월 1일 00:00 UTC(한국시간 09:00)에 실행됩니다. `workflow_dispatch`로 수동 실행도 가능합니다.

## 데이터 출처
금융감독원 전자공시시스템 OpenDART. 전체 재무제표 API는 2015년 이후 사업연도를 제공하며, 보고서 코드는 사업보고서 11011, 반기보고서 11012, 1분기 11013, 3분기 11014입니다.

## 데이터 품질과 해석상 주의
- 자산총계와 부채총계+자본총계의 일치 여부 등 기본 검증을 통과한 행만 대시보드에 게시합니다.
- 검증 실패 행은 CSV 및 보고서에 남겨 원인 확인이 가능하도록 합니다.
- 2010~2014년 원문 XML의 자동 추출값은 보고서별 서식 차이가 있어 원문과 별도 대조가 필요합니다.
- 국내 비교기업 표는 비교 후보 목록이며, 기업별 실제 재무수치 비교는 아직 별도 수집·검증이 필요합니다.
- 본 프로젝트는 학습·분석용이며 투자 권유가 아닙니다.
