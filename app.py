# -*- coding: utf-8 -*-
"""
FastAPI 백엔드 서버 (app.py)

XBRL 뷰어의 REST API 서버입니다.
비즈니스 로직은 XBRLService에 위임하고, 이 모듈은 HTTP 요청/응답 처리만 담당합니다.

엔드포인트:
    GET  /api/status        — 현재 서비스 상태 조회
    POST /api/upload        — XBRL ZIP 파일 업로드 및 로드
    GET  /api/reports       — 보고서 시트 목록 조회
    GET  /api/report        — 특정 시트의 DataFrame 조회
    GET  /api/export        — Excel/CSV 내보내기
    POST /api/analyze       — 사용자 정의 수식 연산
    GET  /                  — 프론트엔드 SPA 제공 (정적 파일)
"""

import os
import shutil
import logging
import tempfile

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.service import XBRLService

# ── 로거 설정 ──
logger = logging.getLogger(__name__)

# ── FastAPI 앱 인스턴스 ──
app = FastAPI(
    title="XBRL Standard Viewer API",
    version="2.0.0",
    description="XBRL 표준 공시 파일을 파싱·분석하는 대화형 뷰어의 백엔드 API",
)

# ── CORS 미들웨어 (로컬 개발용) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 캐시 방지 미들웨어 ──
@app.middleware("http")
async def add_no_cache_header(request, call_next):
    response = await call_next(request)
    # 모든 응답에 대해 브라우저 캐시 비활성화
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# ── 서비스 인스턴스 (앱 전역) ──
service = XBRLService()


# ══════════════════════════════════════════════════════════════════════════
# 앱 이벤트: 서버 시작 시 기본 XBRL 파일 로드
# ══════════════════════════════════════════════════════════════════════════


@app.on_event("startup")
def on_startup():
    """서버 시작 시 기본적으로 파일을 로드하지 않고 대기합니다."""
    logger.info("서버가 시작되었습니다. 사용자의 직접 업로드를 기다립니다.")


# ══════════════════════════════════════════════════════════════════════════
# API 엔드포인트
# ══════════════════════════════════════════════════════════════════════════


@app.get("/api/status")
def get_status():
    """현재 서비스 상태를 반환합니다.

    Returns:
        로드 상태, 파일명, 감지된 보고 기간 등
    """
    return service.get_status()


@app.post("/api/upload")
async def upload_xbrl_zip(file: UploadFile = File(...)):
    """XBRL ZIP 파일을 업로드하고 파싱합니다.

    Args:
        file: 업로드할 ZIP 파일 (multipart/form-data)

    Returns:
        로드 결과 (파일명, 감지된 보고 기간)

    Raises:
        HTTPException 400: ZIP 형식이 아닌 경우
        HTTPException 500: 파싱 실패 시
    """
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="ZIP 파일만 지원됩니다.")

    # 임시 디렉토리에 업로드 파일 저장
    temp_dir = tempfile.mkdtemp()
    temp_file_path = os.path.join(temp_dir, file.filename)

    try:
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 서비스를 통해 파싱
        service.load_from_zip(temp_file_path, file.filename)

        # 임시 ZIP 파일 정리 (파서 내부에서 별도로 해제·관리)
        os.remove(temp_file_path)

        return {
            "status": "success",
            "message": f"XBRL 패키지 {file.filename} 로드 성공",
            "file_name": service.file_name,
            "instants": service.parser.instants,
            "durations": service.parser.durations,
        }
    except Exception as e:
        # 실패 시 임시 디렉토리 정리
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
        logger.error(f"XBRL ZIP 파싱 실패: {e}")
        raise HTTPException(status_code=500, detail=f"XBRL ZIP 파싱 실패: {str(e)}")


@app.get("/api/reports")
def get_reports():
    """보고서 시트 목록을 반환합니다.

    Returns:
        [{role_uri, label, element_count}, ...] 형태의 리스트

    Raises:
        HTTPException 404: 파일이 로드되지 않은 경우
    """
    if not service.is_loaded:
        raise HTTPException(status_code=404, detail="XBRL 파일이 로드되지 않았습니다.")
    try:
        return service.get_reports()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/report")
def get_report(
    role_uri: str = Query(..., description="조회할 역할 URI"),
    active_axis: str = Query(None, description="선택된 활성 축의 concept_id 또는 로컬명"),
):
    """특정 시트의 보고서 데이터를 JSON으로 반환합니다.

    Args:
        role_uri: 조회할 역할 URI (예: http://dart.fss.or.kr/role/.../D210000)
        active_axis: 선택된 활성 축의 concept_id

    Returns:
        {period_type, periods, data} 형태의 딕셔너리

    Raises:
        HTTPException 404: 파일이 로드되지 않은 경우
    """
    if not service.is_loaded:
        raise HTTPException(status_code=404, detail="XBRL 파일이 로드되지 않았습니다.")
    try:
        return service.get_report_data(role_uri, active_axis=active_axis)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/export")
def export_report(
    role_uri: str = Query(..., description="내보낼 역할 URI"),
    format: str = Query("excel", description="형식: excel 또는 csv"),
    active_axis: str = Query(None, description="선택된 활성 축의 concept_id 또는 로컬명"),
):
    """보고서를 Excel 또는 CSV 파일로 내보냅니다.

    Args:
        role_uri: 내보낼 역할 URI
        format: 출력 형식 ('excel' 또는 'csv')
        active_axis: 선택된 활성 축의 concept_id

    Returns:
        파일 다운로드 응답 (StreamingResponse)
    """
    if not service.is_loaded:
        raise HTTPException(status_code=404, detail="XBRL 파일이 로드되지 않았습니다.")
    try:
        stream, media_type, filename = service.export_report(role_uri, format, active_axis=active_axis)
        response = StreamingResponse(stream, media_type=media_type)
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AnalyzeRequest(BaseModel):
    """수식 분석 요청 모델.

    Attributes:
        formulas: {지표명: 수식 문자열} 딕셔너리
    """
    formulas: dict  # {"ratio_name": "concept_A / concept_B", ...}


@app.post("/api/analyze")
def analyze_data(request: AnalyzeRequest):
    """사용자 정의 수식을 평가합니다.

    수식의 변수(Concept ID)를 실제 XBRL 팩트 값으로 치환하여 계산합니다.

    Args:
        request: {formulas: {이름: 수식}} 형태의 요청 본문

    Returns:
        {이름: {formula, consolidated_t, ..., values_used}} 형태의 결과

    Raises:
        HTTPException 404: 파일이 로드되지 않은 경우
    """
    if not service.is_loaded:
        raise HTTPException(status_code=404, detail="XBRL 파일이 로드되지 않았습니다.")
    try:
        return service.analyze(request.formulas)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════
# 정적 파일 서빙 (프론트엔드)
# ══════════════════════════════════════════════════════════════════════════

frontend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "frontend")
)
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="static")
else:
    logger.warning(
        f"프론트엔드 디렉토리를 찾을 수 없습니다: {frontend_path}. "
        f"백엔드 API만 실행됩니다."
    )
