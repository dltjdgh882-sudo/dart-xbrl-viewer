# -*- coding: utf-8 -*-
"""
XBRL 파서 모듈 (xbrl_parser.py)

DART(전자공시시스템) 등에서 내려받은 XBRL 표준 공시 패키지를 파싱하여
2차원 Pandas DataFrame으로 변환하는 핵심 엔진입니다.

지원하는 입력 형식:
    - XBRL 파일이 포함된 디렉토리 경로
    - XBRL 파일이 포함된 ZIP 압축 파일 경로

주요 기능:
    - 한국어/영문 레이블 링크베이스 파싱 (_lab-ko.xml, _lab-en.xml)
    - 표시 링크베이스(Presentation Linkbase) 기반 트리 구조 구축 (_pre.xml)
    - 보고 기간 자동 감지 (당기/전기/전전기)
    - 연결/별도 재무제표 차원 구분
    - 역할(Role) 정의 기반 보고서 시트 목록 생성

사용 예시:
    >>> parser = XBRLParser("path/to/xbrl_package")
    >>> sheets = parser.get_sheet_list()
    >>> df, period_type, periods = parser.get_dataframe_for_role(sheets[0]['role_uri'])
"""

import os
import logging
import zipfile
import tempfile
import xml.etree.ElementTree as ET

import pandas as pd
from datetime import datetime

# ── 로거 설정 ──
logger = logging.getLogger(__name__)


class XBRLParser:
    """XBRL 표준 공시 파일 파서.

    디렉토리 또는 ZIP 파일을 입력받아 내부의 .xbrl, _pre.xml, _lab-ko.xml 등을
    파싱하고, 각 재무제표 역할(role)별로 계층 구조가 반영된 DataFrame을 생성합니다.

    Attributes:
        dir_path (str): 실제 파싱 대상 디렉토리 경로
        namespaces (dict): XML 네임스페이스 프리픽스→URI 매핑
        labels_ko (dict): 한국어 레이블 (concept_id → 텍스트)
        labels_en (dict): 영문 레이블 (concept_id → 텍스트)
        role_definitions (dict): 역할 URI → 한글 정의 텍스트
        contexts (dict): 컨텍스트 ID → 기간/차원 정보
        facts (dict): 개념 ID → 팩트 값 리스트
        instants (list): 감지된 시점형 보고 기간 (최대 3개, 내림차순)
        durations (list): 감지된 기간형 보고 기간 (최대 3개, 내림차순)
    """

    def __init__(self, dir_path_or_zip: str):
        """파서를 초기화하고, 입력 경로의 XBRL 파일을 즉시 파싱합니다.

        Args:
            dir_path_or_zip: XBRL 파일이 포함된 디렉토리 또는 ZIP 파일 경로

        Raises:
            ValueError: 유효하지 않은 경로이거나 필수 파일이 없는 경우
        """
        self._temp_dir = None   # ZIP 해제 시 사용되는 임시 디렉토리
        self.dir_path = None    # 실제 파싱 대상 디렉토리

        # ── 입력 경로 처리: 디렉토리 또는 ZIP ──
        if os.path.isdir(dir_path_or_zip):
            self.dir_path = dir_path_or_zip
        elif zipfile.is_zipfile(dir_path_or_zip):
            self._temp_dir = tempfile.TemporaryDirectory(
                dir=os.path.dirname(os.path.abspath(dir_path_or_zip))
            )
            with zipfile.ZipFile(dir_path_or_zip, 'r') as zip_ref:
                zip_ref.extractall(self._temp_dir.name)
            self.dir_path = self._temp_dir.name
        else:
            raise ValueError(
                f"입력 경로가 유효하지 않습니다 (디렉토리 또는 ZIP 파일이어야 합니다): "
                f"{dir_path_or_zip}"
            )

        # ── 필수 파일 탐색 ──
        self.xbrl_file = None    # .xbrl 본문 파일
        self.xsd_file = None     # .xsd 택소노미 스키마
        self.pre_file = None     # _pre.xml 표시 링크베이스
        self.lab_ko_file = None  # _lab-ko.xml 한국어 레이블
        self.lab_en_file = None  # _lab-en.xml 영문 레이블
        self.cal_file = None     # _cal.xml 계산 링크베이스
        self._scan_directory(self.dir_path)

        # ── XML 네임스페이스 수집 ──
        self.namespaces = self._parse_namespaces(self.xbrl_file)
        # 역인덱스: URI → 프리픽스 (split_tag 최적화용)
        self._uri_to_prefix = {uri: prefix for prefix, uri in self.namespaces.items()}

        # ── 레이블 로드 ──
        self.labels_ko = self._load_labels(self.lab_ko_file) if self.lab_ko_file else {}
        self.labels_en = self._load_labels(self.lab_en_file) if self.lab_en_file else {}

        # ── 역할 정의 로드 (XSD에서 roleType 추출) ──
        self.role_definitions = (
            self._load_role_definitions(self.xsd_file) if self.xsd_file else {}
        )

        # ── 컨텍스트 및 팩트 파싱 ──
        self.contexts = {}  # ctx_id → {period: {...}, dimensions: {...}}
        self.facts = {}     # concept_id → [{contextRef, value, unit, decimals}, ...]
        self._parse_contexts_and_facts()

        # ── 보고 기간 자동 감지 ──
        self.instants = []    # 시점형 기간 (예: ['2025-12-31', '2024-12-31', ...])
        self.durations = []   # 기간형 기간 (예: [('2025-01-01','2025-12-31'), ...])
        self._detect_reporting_periods()

    # ──────────────────────────────────────────────────────────────────────
    # 리소스 관리
    # ──────────────────────────────────────────────────────────────────────

    def close(self):
        """임시 디렉토리를 명시적으로 정리합니다.

        ZIP에서 해제된 임시 파일이 있는 경우 이 메서드를 호출하여
        디스크 공간을 확보할 수 있습니다.
        """
        if self._temp_dir:
            try:
                self._temp_dir.cleanup()
                logger.debug("임시 디렉토리를 정리했습니다.")
            except Exception as e:
                logger.warning(f"임시 디렉토리 정리 중 오류: {e}")
            finally:
                self._temp_dir = None

    def __del__(self):
        """소멸자 — close()가 호출되지 않았을 경우 안전망."""
        self.close()

    def __enter__(self):
        """with 문 지원을 위한 컨텍스트 매니저 진입."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with 문 종료 시 리소스 정리."""
        self.close()
        return False

    # ──────────────────────────────────────────────────────────────────────
    # 내부 파싱 메서드
    # ──────────────────────────────────────────────────────────────────────

    def _scan_directory(self, base_path: str):
        """주어진 디렉토리에서 XBRL 관련 파일을 탐색합니다.

        ZIP 내부에 하위 폴더가 있는 경우도 재귀적으로 탐색합니다.

        Args:
            base_path: 탐색할 루트 디렉토리

        Raises:
            ValueError: 필수 파일(.xbrl, _pre.xml)이 없는 경우
        """
        for root, _dirs, files in os.walk(base_path):
            for f in files:
                full_path = os.path.join(root, f)
                if f.endswith('.xbrl'):
                    self.xbrl_file = full_path
                elif f.endswith('.xsd'):
                    self.xsd_file = full_path
                elif f.endswith('_pre.xml'):
                    self.pre_file = full_path
                elif f.endswith('_lab-ko.xml'):
                    self.lab_ko_file = full_path
                elif f.endswith('_lab-en.xml'):
                    self.lab_en_file = full_path
                elif f.endswith('_cal.xml'):
                    self.cal_file = full_path

        if not self.xbrl_file:
            raise ValueError("패키지 내에 .xbrl 파일을 찾을 수 없습니다.")
        if not self.pre_file:
            raise ValueError("패키지 내에 표시 링크베이스(_pre.xml)를 찾을 수 없습니다.")

        logger.info(f"XBRL 파일 탐색 완료: {os.path.basename(self.xbrl_file)}")

    def _parse_namespaces(self, file_path: str) -> dict:
        """XBRL 본문 파일에서 XML 네임스페이스를 수집합니다.

        파일에 정의되지 않은 표준 네임스페이스가 있으면 폴백(fallback)으로 추가합니다.

        Args:
            file_path: .xbrl 파일 경로

        Returns:
            프리픽스 → URI 매핑 딕셔너리
        """
        ns = {}
        for event, elem in ET.iterparse(file_path, events=('start-ns',)):
            prefix, uri = elem
            ns[prefix] = uri

        # 필수 표준 네임스페이스 폴백
        standard_ns = {
            'xbrli': 'http://www.xbrl.org/2003/instance',
            'xbrldi': 'http://xbrl.org/2006/xbrldi',
            'link': 'http://www.xbrl.org/2003/linkbase',
            'xlink': 'http://www.w3.org/1999/xlink',
            'ifrs-full': 'http://xbrl.ifrs.org/taxonomy/2021-03-24/ifrs-full',
        }
        for k, v in standard_ns.items():
            if k not in ns:
                ns[k] = v

        return ns

    def _split_tag(self, tag: str) -> str:
        """Clark 표기법의 XML 태그를 '프리픽스_로컬이름' 형식으로 변환합니다.

        예: '{http://xbrl.ifrs.org/...}CurrentAssets' → 'ifrs-full_CurrentAssets'

        역인덱스(_uri_to_prefix)를 사용하여 O(1) 조회합니다.

        Args:
            tag: XML 요소의 태그 문자열

        Returns:
            변환된 concept_id 문자열
        """
        if tag.startswith('{'):
            uri, local = tag[1:].split('}', 1)
            prefix = self._uri_to_prefix.get(uri)
            if prefix:
                return f"{prefix}_{local}"
            return local
        return tag

    def _load_labels(self, lab_file: str) -> dict:
        """레이블 링크베이스 XML 파일을 파싱하여 concept_id → 텍스트 매핑을 생성합니다.

        우선순위: terseLabel > standard label > 첫 번째 사용 가능한 레이블

        Args:
            lab_file: 레이블 링크베이스 파일 경로 (*_lab-ko.xml 또는 *_lab-en.xml)

        Returns:
            concept_id → 레이블 텍스트 딕셔너리
        """
        try:
            tree = ET.parse(lab_file)
            root = tree.getroot()
            ns_lab = {
                'link': 'http://www.xbrl.org/2003/linkbase',
                'xlink': 'http://www.w3.org/1999/xlink',
            }

            # 1단계: locator 수집 (xlink:label → concept_id 매핑)
            locators = {}
            for loc in root.findall('.//link:loc', ns_lab):
                label = loc.get('{http://www.w3.org/1999/xlink}label')
                href = loc.get('{http://www.w3.org/1999/xlink}href')
                locators[label] = href.split('#')[-1] if '#' in href else href

            # 2단계: label 요소 수집 (xlink:label → (텍스트, 역할))
            labels = {}
            for label_elem in root.findall('.//link:label', ns_lab):
                label_id = label_elem.get('{http://www.w3.org/1999/xlink}label')
                labels[label_id] = (
                    label_elem.text,
                    label_elem.get('{http://www.w3.org/1999/xlink}role'),
                )

            # 3단계: labelArc를 통해 locator ↔ label 연결
            concept_labels = {}
            for arc in root.findall('.//link:labelArc', ns_lab):
                from_loc = arc.get('{http://www.w3.org/1999/xlink}from')
                to_label = arc.get('{http://www.w3.org/1999/xlink}to')
                cid = locators.get(from_loc)
                label_data = labels.get(to_label)
                if cid and label_data:
                    text, role = label_data
                    if cid not in concept_labels:
                        concept_labels[cid] = {}
                    concept_labels[cid][role] = text

            # 4단계: 역할 우선순위에 따라 최종 레이블 선택
            flat_labels = {}
            std_role = 'http://www.xbrl.org/2003/role/label'
            terse_role = 'http://www.xbrl.org/2003/role/terseLabel'
            for cid, role_dict in concept_labels.items():
                lbl = (
                    role_dict.get(terse_role)
                    or role_dict.get(std_role)
                    or list(role_dict.values())[0]
                )
                flat_labels[cid] = lbl

            logger.info(f"레이블 {len(flat_labels)}개 로드 완료: {os.path.basename(lab_file)}")
            return flat_labels

        except Exception as e:
            logger.error(f"레이블 파일 로드 실패 ({lab_file}): {e}")
            return {}

    def _load_role_definitions(self, xsd_file: str) -> dict:
        """XSD 스키마 파일에서 역할(Role) 정의를 추출합니다.

        각 roleType의 roleURI와 <definition> 텍스트를 매핑합니다.
        (예: 'http://dart.fss.or.kr/role/.../D210000' → '[D210000] 재무상태표')

        Args:
            xsd_file: XSD 스키마 파일 경로

        Returns:
            role_uri → 정의 텍스트 딕셔너리
        """
        try:
            tree = ET.parse(xsd_file)
            root = tree.getroot()
            ns_xsd = {
                'xsd': 'http://www.w3.org/2001/XMLSchema',
                'link': 'http://www.xbrl.org/2003/linkbase',
            }

            roles = {}
            for rt in root.findall('.//link:roleType', ns_xsd):
                role_uri = rt.get('roleURI')
                definition = rt.find('link:definition', ns_xsd)
                if role_uri and definition is not None:
                    roles[role_uri] = definition.text

            logger.info(f"역할 정의 {len(roles)}개 로드 완료")
            return roles

        except Exception as e:
            logger.error(f"역할 정의 로드 실패 ({xsd_file}): {e}")
            return {}

    def _parse_contexts_and_facts(self):
        """XBRL 본문에서 컨텍스트(context)와 팩트(fact)를 파싱합니다.

        컨텍스트: 보고 기간 정보(시점/기간)와 차원 정보를 추출합니다.
        팩트: 각 개념(concept)에 대한 실제 보고 값을 수집합니다.
        """
        tree = ET.parse(self.xbrl_file)
        root = tree.getroot()

        # ── 컨텍스트 파싱 ──
        for ctx in root.findall('.//xbrli:context', self.namespaces):
            ctx_id = ctx.get('id')

            # 보고 기간 추출 (시점형 instant 또는 기간형 duration)
            period = ctx.find('xbrli:period', self.namespaces)
            period_info = {}
            if period is not None:
                instant = period.find('xbrli:instant', self.namespaces)
                if instant is not None:
                    period_info['type'] = 'instant'
                    period_info['date'] = instant.text
                else:
                    start_date = period.find('xbrli:startDate', self.namespaces)
                    end_date = period.find('xbrli:endDate', self.namespaces)
                    if start_date is not None and end_date is not None:
                        period_info['type'] = 'duration'
                        period_info['start_date'] = start_date.text
                        period_info['end_date'] = end_date.text

            # 차원(dimension) 정보 추출 (예: 연결/별도 구분)
            dims = {}
            segment = ctx.find('.//xbrli:segment', self.namespaces)
            if segment is not None:
                for member in segment.findall('xbrldi:explicitMember', self.namespaces):
                    dim = member.get('dimension')
                    member_val = member.text
                    dims[dim] = member_val

            self.contexts[ctx_id] = {
                'period': period_info,
                'dimensions': dims,
            }

        # ── 팩트(Fact) 파싱 ──
        for elem in root:
            context_ref = elem.get('contextRef')
            if not context_ref:
                continue

            concept_id = self._split_tag(elem.tag)
            val_str = elem.text

            # 값 변환: 숫자 → int/float, 비숫자 → 원본 문자열
            if val_str is not None:
                val_str = val_str.strip()
                try:
                    val = float(val_str) if '.' in val_str else int(val_str)
                except ValueError:
                    val = val_str  # 텍스트 블록 등 비숫자 팩트
            else:
                val = None

            unit_ref = elem.get('unitRef')
            decimals = elem.get('decimals')

            if concept_id not in self.facts:
                self.facts[concept_id] = []
            self.facts[concept_id].append({
                'contextRef': context_ref,
                'value': val,
                'unit': unit_ref,
                'decimals': decimals,
            })

        logger.info(
            f"컨텍스트 {len(self.contexts)}개, "
            f"팩트 개념 {len(self.facts)}개 파싱 완료"
        )

    def _detect_reporting_periods(self):
        """보고 기간을 자동으로 감지합니다.

        추가 차원이 없거나, '연결/별도' 축(ConsolidatedAndSeparateFinancialStatementsAxis)
        만 있는 컨텍스트에서 시점형(instant)과 기간형(duration)의 주요 날짜를 추출합니다.

        기간형은 약 1년(350~380일) 범위인 것만 대상으로 합니다.
        결과는 내림차순 정렬하여 최대 3개(당기/전기/전전기)를 저장합니다.
        """
        instant_dates = {}      # date_str → 출현 횟수
        duration_periods = {}   # (start, end) → 출현 횟수

        for _ctx_id, info in self.contexts.items():
            dims = info['dimensions']

            # 추가 차원이 있는 컨텍스트는 건너뜀 (연결/별도 축은 허용)
            has_extra_dims = any(
                'ConsolidatedAndSeparateFinancialStatementsAxis' not in d
                for d in dims
            )
            if has_extra_dims:
                continue

            period = info['period']
            if not period:
                continue

            if period['type'] == 'instant':
                dt = period['date']
                instant_dates[dt] = instant_dates.get(dt, 0) + 1
            elif period['type'] == 'duration':
                p_key = (period['start_date'], period['end_date'])
                # 약 1년(350~380일) 범위인 기간만 대상
                try:
                    d1 = datetime.strptime(p_key[0], "%Y-%m-%d")
                    d2 = datetime.strptime(p_key[1], "%Y-%m-%d")
                    days = (d2 - d1).days
                    if 350 <= days <= 380:
                        duration_periods[p_key] = duration_periods.get(p_key, 0) + 1
                except Exception:
                    pass

        # 내림차순 정렬 후 최대 3개 선택
        self.instants = sorted(instant_dates.keys(), reverse=True)[:3]
        self.durations = sorted(
            duration_periods.keys(), key=lambda x: x[1], reverse=True
        )[:3]

        logger.info(f"감지된 시점형 기간: {self.instants}")
        logger.info(f"감지된 기간형 기간: {self.durations}")

    # ──────────────────────────────────────────────────────────────────────
    # 공개 API 메서드
    # ──────────────────────────────────────────────────────────────────────

    def get_fact_value(
        self,
        concept_id: str,
        target_period,
        is_separate: bool = False,
        period_type: str = 'instant',
    ):
        """특정 개념의 팩트 값을 보고 기간 및 차원 조건으로 조회합니다.

        Args:
            concept_id: 조회할 개념 ID (예: 'ifrs-full_CurrentAssets')
            target_period: 대상 기간
                - 시점형: 날짜 문자열 (예: '2025-12-31')
                - 기간형: (시작일, 종료일) 튜플 (예: ('2025-01-01', '2025-12-31'))
            is_separate: True이면 별도 재무제표, False이면 연결 재무제표
            period_type: 'instant' 또는 'duration'

        Returns:
            팩트 값 (int, float, str) 또는 없으면 None
        """
        concept_facts = self.facts.get(concept_id, [])

        for f in concept_facts:
            ctx = self.contexts.get(f['contextRef'])
            if not ctx:
                continue

            period = ctx['period']

            # 기간 유형 일치 여부 확인
            if period.get('type') != period_type:
                continue

            # 기간 값 매칭
            if period_type == 'instant':
                if period.get('date') != target_period:
                    continue
            else:  # duration
                if (period.get('start_date'), period.get('end_date')) != target_period:
                    continue

            # 차원 매칭 — 추가 차원이 있는 컨텍스트 제외
            dims = ctx['dimensions']
            has_extra_dims = any(
                'ConsolidatedAndSeparateFinancialStatementsAxis' not in d
                for d in dims
            )
            if has_extra_dims:
                continue

            # 연결/별도 구분
            con_axis_val = None
            for dim, val in dims.items():
                if 'ConsolidatedAndSeparateFinancialStatementsAxis' in dim:
                    con_axis_val = val
                    break

            if is_separate:
                if con_axis_val and 'SeparateMember' in con_axis_val:
                    return f['value']
            else:  # 연결 (기본)
                if not con_axis_val or 'ConsolidatedMember' in con_axis_val:
                    return f['value']

        return None

    def get_fact_value_for_equity(
        self,
        concept_id: str,
        target_period,
        is_separate: bool,
        period_type: str,
        member: str,
    ):
        """특정 개념의 팩트 값을 자본변동표의 구성요소(member) 및 기간/차원 조건으로 조회하며, 시점/기간 유형 불일치 시 폴백을 지원합니다."""
        # 1. 원래 요청된 조건으로 조회
        val = self._get_fact_value_for_equity_raw(concept_id, target_period, is_separate, period_type, member)
        if val is not None:
            return val

        # 2. 값 획득 실패 시, 반대 기간 유형(instant <-> duration)으로 교차 조회 폴백
        if period_type == 'instant':
            # instant 종료일에 대응하는 duration 기간 탐색 (예: '2025-12-31' -> '2025-01-01 ~ 2025-12-31')
            matching_dur = next(
                (dur for dur in self.durations if dur[1] == target_period),
                None,
            )
            if matching_dur:
                val = self._get_fact_value_for_equity_raw(concept_id, matching_dur, is_separate, 'duration', member)
                if val is not None:
                    return val
        else:  # duration
            # duration 종료일에 대응하는 instant 시점 탐색 (예: '2025-01-01 ~ 2025-12-31' -> '2025-12-31')
            target_inst = target_period[1]
            val = self._get_fact_value_for_equity_raw(concept_id, target_inst, is_separate, 'instant', member)
            if val is not None:
                return val

        return None

    def _get_fact_value_for_equity_raw(
        self,
        concept_id: str,
        target_period,
        is_separate: bool,
        period_type: str,
        member: str,
    ):
        """특정 개념의 팩트 값을 지정된 조건으로 정밀 조회합니다. (폴백 없음)"""
        concept_facts = self.facts.get(concept_id, [])

        for f in concept_facts:
            ctx = self.contexts.get(f['contextRef'])
            if not ctx:
                continue

            period = ctx['period']

            # 기간 유형 일치 여부 확인
            if period.get('type') != period_type:
                continue

            # 기간 값 매칭
            if period_type == 'instant':
                if period.get('date') != target_period:
                    continue
            else:  # duration
                if (period.get('start_date'), period.get('end_date')) != target_period:
                    continue

            # 차원 매칭
            dims = ctx['dimensions']

            # 1. Consolidated/Separate match
            con_axis_val = None
            for dim, val in dims.items():
                if 'ConsolidatedAndSeparateFinancialStatementsAxis' in dim:
                    con_axis_val = val
                    break

            if is_separate:
                if not (con_axis_val and 'SeparateMember' in con_axis_val):
                    continue
            else:  # 연결 (기본)
                if con_axis_val and 'SeparateMember' in con_axis_val:
                    continue

            # 2. ComponentsOfEquityAxis match
            equity_axis_val = None
            for dim, val in dims.items():
                if 'ComponentsOfEquityAxis' in dim:
                    equity_axis_val = val
                    break

            member_local = member.split('_')[-1] if '_' in member else member
            member_local = member_local.split(':')[-1] if ':' in member_local else member_local
            
            if member_local in ['EquityMember', 'EquityMemberTotal', 'TotalEquity']:
                if equity_axis_val is not None:
                    val_local = equity_axis_val.split(':')[-1]
                    if val_local != 'EquityMember':
                        continue
            else:
                if equity_axis_val is None:
                    continue
                val_local = equity_axis_val.split(':')[-1]
                if val_local != member_local:
                    continue

            # 3. 추가적인 기타 차원은 제외 (예: ClassesOfShareCapitalAxis 등)
            has_other_extra_dims = False
            for dim in dims:
                if 'ConsolidatedAndSeparateFinancialStatementsAxis' not in dim and 'ComponentsOfEquityAxis' not in dim:
                    has_other_extra_dims = True
                    break
            if has_other_extra_dims:
                continue

            return f['value']

        return None

    def get_fact_value_for_dimensional(
        self,
        concept_id: str,
        target_period,
        is_separate: bool,
        period_type: str,
        dimension_filters: dict,
    ):
        """범용 다축 테이블에서 지정된 차원 필터 조건에 맞는 팩트 값을 조회합니다.

        시점/기간 유형 불일치 시 교차 폴백을 지원합니다.

        Args:
            concept_id: 조회할 개념 ID
            target_period: 대상 기간 (instant: str, duration: tuple)
            is_separate: True이면 별도, False이면 연결
            period_type: 'instant' 또는 'duration'
            dimension_filters: {axis_concept_id: member_concept_id} 매핑
                (ConsolidatedAndSeparate 축은 포함하지 않음)

        Returns:
            팩트 값 또는 None
        """
        # 1차: 원래 요청 조건으로 조회
        val = self._get_fact_value_for_dimensional_raw(
            concept_id, target_period, is_separate, period_type, dimension_filters
        )
        if val is not None:
            return val

        # 2차: 반대 기간 유형 폴백
        if period_type == 'instant':
            matching_dur = next(
                (dur for dur in self.durations if dur[1] == target_period),
                None,
            )
            if matching_dur:
                val = self._get_fact_value_for_dimensional_raw(
                    concept_id, matching_dur, is_separate, 'duration', dimension_filters
                )
                if val is not None:
                    return val
        else:  # duration
            target_inst = target_period[1]
            val = self._get_fact_value_for_dimensional_raw(
                concept_id, target_inst, is_separate, 'instant', dimension_filters
            )
            if val is not None:
                return val

        return None

    def _get_fact_value_for_dimensional_raw(
        self,
        concept_id: str,
        target_period,
        is_separate: bool,
        period_type: str,
        dimension_filters: dict,
    ):
        """범용 다축 팩트 값 정밀 조회 (폴백 없음).

        Args:
            dimension_filters: {axis_local_suffix: member_local_suffix} 매핑
                axis_local_suffix는 축 concept_id의 로컬 부분 매칭에 사용됩니다.
        """
        concept_facts = self.facts.get(concept_id, [])

        # 필터에서 사용할 축 이름의 로컬 부분을 미리 계산
        # 축 concept_id는 'prefix_LocalName' 형식 (예: ifrs-full_GeographicalAreasAxis)
        # 컨텍스트 dimension key는 'prefix:LocalName' 형식 (예: ifrs-full:GeographicalAreasAxis)
        # → 양쪽 모두에서 LocalName을 추출하여 매칭
        def _extract_local(s):
            """concept_id 또는 dimension key에서 로컬 이름을 추출합니다."""
            # 먼저 colon으로 분리 (prefix:LocalName)
            if ':' in s:
                return s.split(':', 1)[-1]
            # 그 다음 underscore로 분리 (prefix_LocalName)
            # 단, prefix에는 하이픈이 포함될 수 있음 (ifrs-full_XXX, dart_XXX)
            # → 첫 번째 underscore만 사용
            if '_' in s:
                return s.split('_', 1)[-1]
            return s

        filter_axis_locals = {}
        for ax_key, member_val in dimension_filters.items():
            ax_local = _extract_local(ax_key)
            m_local = _extract_local(member_val)
            filter_axis_locals[ax_local] = m_local

        for f in concept_facts:
            ctx = self.contexts.get(f['contextRef'])
            if not ctx:
                continue

            period = ctx['period']
            if period.get('type') != period_type:
                continue

            if period_type == 'instant':
                if period.get('date') != target_period:
                    continue
            else:
                if (period.get('start_date'), period.get('end_date')) != target_period:
                    continue

            dims = ctx['dimensions']

            # 1. Consolidated/Separate 매칭
            con_axis_val = None
            for dim, val in dims.items():
                if 'ConsolidatedAndSeparateFinancialStatementsAxis' in dim:
                    con_axis_val = val
                    break

            if is_separate:
                if not (con_axis_val and 'SeparateMember' in con_axis_val):
                    continue
            else:
                if con_axis_val and 'SeparateMember' in con_axis_val:
                    continue

            # 2. 다축 차원 필터 매칭
            # 컨텍스트의 비표준 차원들을 수집
            ctx_extra_dims = {}
            for dim, val in dims.items():
                if 'ConsolidatedAndSeparateFinancialStatementsAxis' in dim:
                    continue
                dim_local = dim.split(':')[-1] if ':' in dim else dim
                val_local = val.split(':')[-1] if ':' in val else val
                ctx_extra_dims[dim_local] = val_local

            # 필터에 지정된 모든 축이 컨텍스트에 매칭되는지 확인
            matched = True
            for ax_local, m_local in filter_axis_locals.items():
                ctx_val = ctx_extra_dims.get(ax_local)
                if ctx_val is None or ctx_val != m_local:
                    matched = False
                    break

            if not matched:
                continue

            # 3. 컨텍스트에 필터에 없는 추가 차원이 있으면 제외
            #    (더 세분화된 차원의 팩트와 혼동 방지)
            if len(ctx_extra_dims) != len(filter_axis_locals):
                continue

            return f['value']


        return None

    def get_sheet_list(self) -> list:
        """표시 링크베이스에서 보고서 시트 목록을 추출합니다.

        각 시트는 하나의 presentationLink 요소에 대응하며,
        역할 URI, 한글 정의, 포함된 요소 개수를 반환합니다.

        Returns:
            보고서 시트 딕셔너리 리스트.
            각 항목: {'role_uri': str, 'label': str, 'element_count': int}
        """
        tree = ET.parse(self.pre_file)
        root = tree.getroot()

        roles = []
        for pl in root.findall('.//link:presentationLink', self.namespaces):
            role_uri = pl.get('{http://www.w3.org/1999/xlink}role')
            label = self.role_definitions.get(role_uri, role_uri)

            # 빈 시트는 제외
            loc_count = len(pl.findall('link:loc', self.namespaces))
            if loc_count > 0:
                roles.append({
                    'role_uri': role_uri,
                    'label': label,
                    'element_count': loc_count,
                })

        return roles

    def get_dataframe_for_role(self, role_uri: str, active_axis: str = None):
        """특정 역할(role)에 대한 계층적 DataFrame을 생성합니다.

        표시 링크베이스의 부모-자식 관계를 DFS로 순회하여, 깊이(depth)와
        부모 정보가 포함된 플랫 DataFrame을 반환합니다.

        Args:
            role_uri: 조회할 역할 URI
                (예: 'http://dart.fss.or.kr/role/ifrs/dart_2024-06-30_role-D210000')
            active_axis: 선택된 활성 축의 concept_id

        Returns:
            (DataFrame, period_type, active_periods) 튜플
            - DataFrame: 계층 구조가 반영된 2D 데이터프레임
            - period_type: 'instant' 또는 'duration'
            - active_periods: 사용된 보고 기간 리스트
        """
        tree = ET.parse(self.pre_file)
        root = tree.getroot()

        # ── 해당 역할의 presentationLink 요소 찾기 ──
        p_link = None
        for pl in root.findall('.//link:presentationLink', self.namespaces):
            if pl.get('{http://www.w3.org/1999/xlink}role') == role_uri:
                p_link = pl
                break

        if p_link is None:
            return pd.DataFrame(), 'instant', []

        # ── locator 파싱: xlink:label → concept_id ──
        locs = {}
        for loc in p_link.findall('link:loc', self.namespaces):
            lbl = loc.get('{http://www.w3.org/1999/xlink}label')
            href = loc.get('{http://www.w3.org/1999/xlink}href')
            cid = href.split('#')[-1] if '#' in href else href
            locs[lbl] = cid

        # ── presentationArc 파싱: 부모-자식 관계 구축 ──
        parent_child = {}   # parent_id → [child_id, ...]
        child_parent = {}   # child_id → parent_id

        for arc in p_link.findall('link:presentationArc', self.namespaces):
            from_lbl = arc.get('{http://www.w3.org/1999/xlink}from')
            to_lbl = arc.get('{http://www.w3.org/1999/xlink}to')
            order = float(arc.get('order', '0'))

            parent_id = locs.get(from_lbl)
            child_id = locs.get(to_lbl)

            if parent_id and child_id:
                if parent_id not in parent_child:
                    parent_child[parent_id] = []
                parent_child[parent_id].append((child_id, order))
                child_parent[child_id] = parent_id

        # 자식 노드를 order 순서로 정렬
        for p_id in parent_child:
            parent_child[p_id].sort(key=lambda x: x[1])
            parent_child[p_id] = [cid for cid, _order in parent_child[p_id]]

        # 루트 노드 결정 (부모가 없는 노드)
        roots = sorted(set(parent_child.keys()) - set(child_parent.keys()))

        # ── 기간 유형 자동 판별 (instant vs duration) ──
        all_concepts = list(locs.values())
        instant_count = 0
        duration_count = 0

        for concept in all_concepts[:10]:  # 상위 10개 개념으로 샘플링
            concept_facts = self.facts.get(concept, [])
            for f in concept_facts:
                ctx = self.contexts.get(f['contextRef'])
                if ctx and ctx['period']:
                    if ctx['period']['type'] == 'instant':
                        instant_count += 1
                    else:
                        duration_count += 1

        is_instant = instant_count >= duration_count
        period_type = 'instant' if is_instant else 'duration'
        active_periods = self.instants if is_instant else self.durations

        # ── 자본변동표 축(Axis) 및 차원 탐색 ──
        equity_axis_node = None
        for cid in locs.values():
            if 'ComponentsOfEquityAxis' in cid:
                equity_axis_node = cid
                break

        has_equity_axis = equity_axis_node is not None

        if has_equity_axis:
            # 자본의 구성요소 멤버(컬럼) 추출
            equity_members = []
            def collect_members(node):
                if node in parent_child:
                    for child in parent_child[node]:
                        if child not in equity_members:
                            equity_members.append(child)
                        collect_members(child)
            collect_members(equity_axis_node)

            # 연결/별도 여부 감지
            is_separate = any('SeparateMember' in cid for cid in locs.values())

            # 다이내믹 컬럼 메타 정보 구성
            columns_meta = []
            for member in equity_members:
                for idx, period_key in enumerate(['t', 't1', 't2']):
                    if idx < len(active_periods):
                        p = active_periods[idx]
                        p_str = f"{p[0]} ~ {p[1]}" if isinstance(p, tuple) else str(p)
                        col_key = f"{member}_{period_key}"
                        # [멤버] 단어 정제
                        m_label = self.labels_ko.get(member, member)
                        m_label = m_label.replace(' [멤버]', '').replace(' [member]', '').replace('[멤버]', '').replace('[member]', '').strip()
                        columns_meta.append({
                            "key": col_key,
                            "label": m_label,
                            "member": member,
                            "period": period_key,
                            "period_str": p_str
                        })

            # 테이블/축/멤버 노드들을 하나의 세트로 묶어 행에서 제외
            table_axis_member_nodes = {equity_axis_node}
            for cid in locs.values():
                if 'Table' in cid or 'Axis' in cid:
                    table_axis_member_nodes.add(cid)

            # 모든 하위 멤버들 재귀적 수집
            for node in list(table_axis_member_nodes):
                def collect_descendants(n):
                    if n in parent_child:
                        for child in parent_child[n]:
                            if child not in table_axis_member_nodes:
                                table_axis_member_nodes.add(child)
                                collect_descendants(child)
                collect_descendants(node)

            # DFS 순회로 자본변동표 행(Row) 구축 (Table/Axis/Member는 제외)
            rows = []
            visited = set()

            def traverse_equity(node: str, depth: int, pid: str):
                if node in visited:
                    return
                visited.add(node)

                if node in table_axis_member_nodes:
                    return

                lbl_ko = self.labels_ko.get(node, node)
                lbl_en = self.labels_en.get(node, node)

                vals = {}
                for col_meta in columns_meta:
                    col_key = col_meta['key']
                    member = col_meta['member']
                    period_key = col_meta['period']
                    period_idx = ['t', 't1', 't2'].index(period_key)

                    if period_idx < len(active_periods):
                        p = active_periods[period_idx]
                        vals[col_key] = self.get_fact_value_for_equity(
                            node, p, is_separate=is_separate, period_type=period_type, member=member
                        )
                    else:
                        vals[col_key] = None

                is_abstract = node.endswith('Abstract')
                if not is_abstract:
                    has_value = any(vals.get(col_meta['key']) is not None for col_meta in columns_meta)
                    if not has_value and node in parent_child:
                        is_abstract = True

                row_dict = {
                    'concept_id': node,
                    'parent_id': pid,
                    'label_ko': lbl_ko,
                    'label_en': lbl_en,
                    'depth': depth,
                    'is_abstract': is_abstract,
                }
                row_dict.update(vals)
                rows.append(row_dict)

                if node in parent_child:
                    for child in parent_child[node]:
                        if child not in table_axis_member_nodes:
                            traverse_equity(child, depth + 1, node)

            for r in roots:
                traverse_equity(r, 0, None)

            df = pd.DataFrame(rows)
            df.attrs['is_equity'] = True
            df.attrs['equity_columns'] = columns_meta
            return df, period_type, active_periods

        # ── 범용 다축(Dimensional) 주석 테이블 감지 및 피벗 ──
        IGNORE_AXES = {
            'ConsolidatedAndSeparateFinancialStatementsAxis',
            'ComponentsOfEquityAxis',
        }

        # Presentation 트리에서 비표준 축(Axis) 탐색
        dim_axes = []
        for cid in locs.values():
            if 'Axis' in cid and not any(ignore in cid for ignore in IGNORE_AXES):
                dim_axes.append(cid)

        if dim_axes:
            # 연결/별도 여부 감지
            is_separate = any('SeparateMember' in cid for cid in locs.values())

            # 축별 멤버 수집 (Presentation Linkbase 순서 유지)
            axes_meta = []
            all_axis_member_nodes = set()

            for axis_node in dim_axes:
                members = []
                def collect_dim_members(node, depth=0, parent_id=None):
                    if node in parent_child:
                        for child in parent_child[node]:
                            if child not in [n for n, _, _, _ in members]:
                                m_label = self.labels_ko.get(child, child)
                                m_label = (m_label
                                    .replace(' [멤버]', '').replace(' [member]', '')
                                    .replace('[멤버]', '').replace('[member]', '')
                                    .strip())
                                members.append((child, m_label, depth, parent_id))
                            collect_dim_members(child, depth + 1, child)
                collect_dim_members(axis_node, depth=0, parent_id=axis_node)

                axis_label = self.labels_ko.get(axis_node, axis_node)
                axis_label = (axis_label
                     .replace(' [축]', '').replace(' [axis]', '')
                     .replace('[축]', '').replace('[axis]', '')
                     .strip())

                axes_meta.append({
                    'axis': axis_node,
                    'axis_label': axis_label,
                    'members': members,  # [(concept_id, label, depth, parent_id), ...]
                })

                # 축/멤버 노드를 행 데이터에서 제외할 세트에 추가
                all_axis_member_nodes.add(axis_node)
                for m_id, _, _, _ in members:
                    all_axis_member_nodes.add(m_id)

            # Table/LineItems 노드도 행에서 제외
            table_nodes = set()
            for cid in locs.values():
                if 'Table' in cid:
                    table_nodes.add(cid)
                    all_axis_member_nodes.add(cid)

            # LineItems와 Axis 매핑 관계 식별
            lineitems_to_axes = {}
            for cid in locs.values():
                if 'LineItems' in cid:
                    parent = child_parent.get(cid)
                    if parent:
                        siblings = parent_child.get(parent, [])
                        for sib in siblings:
                            if 'Table' in sib:
                                table_axes = []
                                def find_axes(n):
                                    if 'Axis' in n and not any(ignore in n for ignore in IGNORE_AXES):
                                        table_axes.append(n)
                                    if n in parent_child:
                                        for child in parent_child[n]:
                                            find_axes(child)
                                find_axes(sib)
                                if table_axes:
                                    lineitems_to_axes[cid] = table_axes

            # 특정 노드의 관련 축 목록 반환 헬퍼
            def get_node_axes(node):
                curr = node
                while curr:
                    if 'LineItems' in curr:
                        return lineitems_to_axes.get(curr, [])
                    curr = child_parent.get(curr)
                return []

            # 특정 노드 하위에 활성화 대상 LineItems가 있는지 검사 헬퍼
            def has_active_lineitems_descendant(node, active_axis_id):
                if 'LineItems' in node:
                    return active_axis_id in lineitems_to_axes.get(node, [])
                for child in parent_child.get(node, []):
                    if has_active_lineitems_descendant(child, active_axis_id):
                        return True
                return False

            # 활성 축 결정 (기본값: 첫 번째 축, 사용자 지정 시 해당 축 선택)
            column_axis = None
            if active_axis:
                for ax in axes_meta:
                    if ax['axis'] == active_axis or ax['axis'].split(':')[-1] == active_axis or ax['axis'].split('_')[-1] == active_axis:
                        column_axis = ax
                        break
            if not column_axis:
                column_axis = axes_meta[0]

            # 동적 컬럼 메타 정보 구성 (member × period)
            columns_meta = []
            for member_id, member_label, _, _ in column_axis['members']:
                for idx, period_key in enumerate(['t', 't1', 't2']):
                    if idx < len(active_periods):
                        p = active_periods[idx]
                        p_str = f"{p[0]} ~ {p[1]}" if isinstance(p, tuple) else str(p)
                        col_key = f"{member_id}_{period_key}"
                        columns_meta.append({
                            "key": col_key,
                            "label": member_label,
                            "member": member_id,
                            "period": period_key,
                            "period_str": p_str,
                            "axis": column_axis['axis'],
                        })

            # DFS 순회로 행 구축 (Table/Axis/Member 제외, 활성 축에 해당하는 데이터만 필터링)
            rows = []
            visited = set()

            def traverse_dimensional(node: str, depth: int, pid: str):
                if node in visited:
                    return
                visited.add(node)

                # 축/멤버/테이블 노드는 행에서 제외
                if node in all_axis_member_nodes:
                    return

                # 노드 필터링: 활성 축과 일치하지 않는 테이블 영역 제외
                node_axes = get_node_axes(node)
                if node_axes:
                    if column_axis['axis'] not in node_axes:
                        return
                else:
                    # 테이블 외부 노드인 경우, 활성 테이블로 이어지는 조상 노드만 유지
                    if not has_active_lineitems_descendant(node, column_axis['axis']):
                        return

                lbl_ko = self.labels_ko.get(node, node)
                lbl_en = self.labels_en.get(node, node)

                vals = {}
                for col_meta in columns_meta:
                    col_key = col_meta['key']
                    member = col_meta['member']
                    period_key = col_meta['period']
                    period_idx = ['t', 't1', 't2'].index(period_key)

                    if period_idx < len(active_periods):
                        p = active_periods[period_idx]
                        dim_filter = {column_axis['axis']: member}
                        vals[col_key] = self.get_fact_value_for_dimensional(
                            node, p, is_separate=is_separate,
                            period_type=period_type, dimension_filters=dim_filter,
                        )
                    else:
                        vals[col_key] = None

                is_abstract = node.endswith('Abstract')
                if not is_abstract:
                    has_value = any(
                        vals.get(col_meta['key']) is not None
                        for col_meta in columns_meta
                    )
                    if not has_value and node in parent_child:
                        is_abstract = True

                row_dict = {
                    'concept_id': node,
                    'parent_id': pid,
                    'label_ko': lbl_ko,
                    'label_en': lbl_en,
                    'depth': depth,
                    'is_abstract': is_abstract,
                }
                row_dict.update(vals)
                rows.append(row_dict)

                if node in parent_child:
                    for child in parent_child[node]:
                        if child not in all_axis_member_nodes:
                            traverse_dimensional(child, depth + 1, node)

            for r in roots:
                traverse_dimensional(r, 0, None)

            df = pd.DataFrame(rows)
            df.attrs['is_dimensional'] = True
            df.attrs['dimensional_columns'] = columns_meta
            df.attrs['dimensional_axes'] = [
                {
                    'axis': ax['axis'],
                    'axis_label': ax['axis_label'],
                    'members': [
                        {
                            'id': m_id,
                            'label': m_lbl,
                            'depth': m_depth,
                            'parent_id': m_pid
                        }
                        for m_id, m_lbl, m_depth, m_pid in ax['members']
                    ],
                }
                for ax in axes_meta
            ]
            return df, period_type, active_periods

        # ── 일반적인 보고서 (재무상태표, 손익계산서 등): 기존 DFS 로직 유지 ──
        rows = []
        visited = set()

        def traverse(node: str, depth: int, pid: str):
            """재귀적으로 트리 노드를 순회하며 DataFrame 행을 생성합니다."""
            if node in visited:
                return  # 순환 참조 방어
            visited.add(node)

            lbl_ko = self.labels_ko.get(node, node)
            lbl_en = self.labels_en.get(node, node)

            # 각 보고 기간(당기/전기/전전기)에 대해 연결/별도 값 조회
            vals = {}
            keys = ['t', 't1', 't2']
            for i, key in enumerate(keys):
                if i < len(active_periods):
                    p = active_periods[i]
                    vals[f'con_{key}'] = self.get_fact_value(
                        node, p, is_separate=False, period_type=period_type
                    )
                    vals[f'sep_{key}'] = self.get_fact_value(
                        node, p, is_separate=True, period_type=period_type
                    )
                else:
                    vals[f'con_{key}'] = None
                    vals[f'sep_{key}'] = None

            # 추상(Abstract) 노드 판별: 개념 ID가 'Abstract'로 끝나는지 확인
            is_abstract = node.endswith('Abstract')
            # 추상이 아니더라도, 자기 자신의 값이 없으면 추상으로 간주
            if not is_abstract:
                has_value = any(
                    vals.get(f'con_{k}') is not None or vals.get(f'sep_{k}') is not None
                    for k in keys
                )
                if not has_value and node in parent_child:
                    is_abstract = True

            rows.append({
                'concept_id': node,
                'parent_id': pid,
                'label_ko': lbl_ko,
                'label_en': lbl_en,
                'depth': depth,
                'is_abstract': is_abstract,
                'consolidated_t': vals['con_t'],
                'consolidated_t1': vals['con_t1'],
                'consolidated_t2': vals['con_t2'],
                'separate_t': vals['sep_t'],
                'separate_t1': vals['sep_t1'],
                'separate_t2': vals['sep_t2'],
            })

            # 자식 노드 재귀 순회
            if node in parent_child:
                for child in parent_child[node]:
                    traverse(child, depth + 1, node)

        for r in roots:
            traverse(r, 0, None)

        df = pd.DataFrame(rows)
        return df, period_type, active_periods
