# OCR Trainer

PDF OCR 결과인 TXT를 사람이 문서/블록 단위로 검수하고, 수정 이력과 학습용 데이터를 생성하는 로컬 Streamlit 도구입니다. 특정 문서명이나 서식에 종속되지 않으며 자동 탐지는 항상 후보로만 사용됩니다.

## 설치 및 실행

Python 3.11 환경에서 다음을 실행합니다.

```powershell
cd C:\repos\OCR-TRAINER
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## 작업 흐름

1. TXT를 업로드하고 `업로드 분석`을 누릅니다. UTF-8/UTF-8-SIG/CP949/EUC-KR을 자동 판별합니다.
2. 왼쪽의 원문과 문서 경계 후보를 참고해 중앙에서 문서 제목·유형 및 블록을 검수합니다.
3. `EDIT`, `MOVE`, `MERGE`, `SPLIT`, `DELETE`, `INSERT`, `KEEP`, `DOC_SPLIT`, `DOC_MERGE`를 사용합니다. 각 작업은 즉시 상태 JSON과 수정 JSONL에 저장됩니다.
4. `검수 완료·내보내기`를 누르면 AFTER TXT, 문서/파일 학습 JSONL, Excel 보고서가 생성됩니다.

같은 내용의 파일을 다시 업로드하면 파일 내용 해시로 동일한 `file_id`를 찾고 기존 작업을 복구합니다. 파일명이 같아도 내용이 다르면 별도 작업으로 생성됩니다.

## 데이터 구조

```text
data/
├─ before/                 # 변경하지 않는 업로드 원본(UTF-8 정규화)
├─ after/
│  ├─ by_file/             # 문서 헤더를 포함한 전체 AFTER TXT
│  └─ by_document/         # document_id별 AFTER TXT
├─ logs_jsonl/             # 수정 이력 및 학습 pair
├─ logs_excel/             # Summary/Documents/Change_Log/Block_Status 보고서
├─ working_state/          # 자동 복구용 현재 상태
└─ reports/
```

`working_state`는 임시 파일을 거친 원자적 교체 방식으로 저장합니다. 원본 파일은 절대 수정하지 않습니다. JSONL은 수정 버튼을 누를 때마다 한 줄씩 추가됩니다.

## 자동 탐지 원칙

문서 경계는 제목 형태, 일반 문서 키워드, 독립 날짜, 연속 빈 줄, 페이지 표식 등을 점수화합니다. 강한 후보만 초기 분리에 사용하고, 약한 후보와 중간 후보는 UI에 근거와 함께 표시합니다. 최종 경계와 자유 형식 문서 유형은 사용자가 수정합니다.

## 산출물

- `data/after/by_file/{file_id}_after.txt`
- `data/after/by_document/{file_id}/{document_id}_after.txt`
- `data/logs_jsonl/{file_id}_corrections.jsonl`
- `data/logs_jsonl/{file_id}_document_pairs.jsonl`
- `data/logs_jsonl/{file_id}_file_pair.jsonl`
- `data/logs_excel/{file_id}_change_log.xlsx`

Excel은 `Summary`, `Documents`, `Change_Log`, `Block_Status` 시트로 구성됩니다.

