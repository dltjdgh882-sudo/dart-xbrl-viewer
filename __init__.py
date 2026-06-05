# -*- coding: utf-8 -*-
"""
XBRL Viewer 백엔드 패키지.

이 패키지는 XBRL(eXtensible Business Reporting Language) 표준 공시 파일을
파싱·분석하기 위한 핵심 모듈을 제공합니다.

주요 구성:
    - xbrl_parser : XBRL 원시 XML 파일을 파싱하여 2D DataFrame으로 변환
    - service     : 파서 인스턴스 관리 및 비즈니스 로직 캡슐화 (서비스 레이어)
    - app         : FastAPI 기반 REST API 서버 (프론트엔드 제공용)

사용 예시 (CLI / 외부 모듈):
    >>> from backend.service import XBRLService
    >>> svc = XBRLService()
    >>> svc.load_from_zip("path/to/report.zip")
    >>> reports = svc.get_reports()
    >>> df = svc.get_report_dataframe("role_uri")
"""

from backend.xbrl_parser import XBRLParser
from backend.service import XBRLService

__all__ = ["XBRLParser", "XBRLService"]
