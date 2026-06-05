// ==========================================================================
// XBRL Viewer — 프론트엔드 애플리케이션 (app.js)
//
// 이 파일은 XBRL 뷰어 SPA의 전체 클라이언트 로직을 담당합니다.
// 주요 기능:
//   - 백엔드 API와의 통신 (보고서 목록, 데이터 조회, 업로드, 분석)
//   - 계층 트리 그리드 렌더링 (접기/펼치기, 깊이별 들여쓰기)
//   - 2차원 DataFrame 뷰 (플랫 테이블)
//   - 재무비율 자동 계산 및 사용자 정의 수식 연산
//   - 테마 전환 (다크/라이트), 숫자 단위 스케일 조절
//   - Excel/CSV 내보내기, ZIP 드래그앤드롭 업로드
// ==========================================================================

// ==========================================================================
// 앱 상태 및 설정
// ==========================================================================

/** @type {Object} 전역 앱 상태 */
let appState = {
    fileLoaded: false,          // XBRL 파일 로드 여부
    fileName: "",               // 로드된 파일명
    reports: [],                // 보고서 시트 목록 (API 응답 캐시)
    activeReportUri: null,      // 현재 선택된 시트의 역할 URI
    activeReportData: null,     // 현재 시트의 데이터 (API 응답 캐시)
    collapsedNodes: new Set(),  // 접힌(collapsed) 노드의 concept_id 집합
    selectedConceptId: null,    // 트리에서 선택된 행의 concept_id
    currentScale: 1000000000,   // 숫자 표시 단위 (기본: 십억원)
    theme: 'light',             // 현재 테마 ('dark' 또는 'light')
    searchQuery: '',            // 트리 내 검색어
    sheetSearchQuery: '',       // 사이드바 시트 검색어
    parentMap: null,            // concept_id → parent_id 빠른 조회 맵
    reportType: 'consolidated', // 'consolidated' 또는 'separate'
    activeAxis: null,           // 현재 선택된 활성 축의 concept_id
    pivotSwapped: false,        // 축 전환(피벗) 여부
};

/** API 기본 URL (현재 호스트 기준) */
const BASE_URL = window.location.origin;

/**
 * 사전 정의된 재무비율 수식.
 * 분석 탭의 대시보드 카드에 자동 표시됩니다.
 */
const PREDEFINED_RATIOS = {
    current_ratio: {
        name: "유동비율 (Current Ratio)",
        expr: "ifrs-full_CurrentAssets / ifrs-full_CurrentLiabilities",
    },
    debt_ratio: {
        name: "부채비율 (Debt-to-Equity)",
        expr: "ifrs-full_Liabilities / ifrs-full_Equity",
    },
    op_margin: {
        name: "영업이익률 (Operating Margin)",
        // DART 표준에서는 ifrs-full_OperatingProfitLoss 대신
        // dart_OperatingIncomeLoss를 사용함
        expr: "dart_OperatingIncomeLoss / ifrs-full_Revenue",
    },
};

// ==========================================================================
// 앱 초기화
// ==========================================================================

document.addEventListener("DOMContentLoaded", () => {
    initTheme();        // 테마 초기화 (localStorage에서 복원)
    checkFileStatus();  // 백엔드 상태 확인 및 보고서 로드
    setupEventListeners(); // 이벤트 리스너 등록
});

// ==========================================================================
// 테마 관리 (다크/라이트 모드)
// ==========================================================================

/** 저장된 테마를 복원합니다. */
function initTheme() {
    const savedTheme = localStorage.getItem('theme') || 'light';
    appState.theme = savedTheme;
    if (savedTheme === 'light') {
        document.body.classList.remove('dark-mode');
        document.body.classList.add('light-mode');
    } else {
        document.body.classList.add('dark-mode');
        document.body.classList.remove('light-mode');
    }
    updateThemeIcon(savedTheme);
}

/** 다크 ↔ 라이트 테마를 전환합니다. */
function toggleTheme() {
    if (appState.theme === 'dark') {
        appState.theme = 'light';
        document.body.classList.remove('dark-mode');
        document.body.classList.add('light-mode');
    } else {
        appState.theme = 'dark';
        document.body.classList.add('dark-mode');
        document.body.classList.remove('light-mode');
    }
    localStorage.setItem('theme', appState.theme);
    updateThemeIcon(appState.theme);
}

/** 테마 토글 버튼의 아이콘을 업데이트합니다. */
function updateThemeIcon(theme) {
    const icon = document.querySelector('#theme-toggle i');
    if (icon) {
        icon.className = theme === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
    }
}

// ==========================================================================
// API 통신 — 백엔드와의 데이터 교환
// ==========================================================================

/**
 * 서버 상태를 확인하고, 파일이 로드되어 있으면 보고서 목록을 불러옵니다.
 * 파일이 없으면 업로드 모달을 엽니다.
 */
async function checkFileStatus() {
    try {
        const res = await fetch(`${BASE_URL}/api/status`);
        const status = await res.json();

        if (status.file_loaded) {
            appState.fileLoaded = true;
            appState.fileName = status.file_name;
            document.getElementById("current-filename").textContent = status.file_name;
            loadReportList();
        } else {
            appState.fileLoaded = false;
            showUploadPrompt();
            openUploadModal();
        }
    } catch (e) {
        console.error("서버 상태 확인 실패:", e);
        appState.fileLoaded = false;
        showUploadPrompt();
        openUploadModal();
    }
}

/**
 * 파일이 로드되지 않았을 때 메인 화면에 파일 업로드 안내 플레이스홀더를 표시합니다.
 */
function showUploadPrompt() {
    document.getElementById("current-filename").textContent = "파일 없음";
    document.getElementById("active-sheet-title").textContent = "XBRL 파일 업로드 필요";
    document.getElementById("active-sheet-subtitle").textContent = "상단의 'Upload ZIP Package' 버튼을 누르거나 파일을 드래그하여 업로드하세요.";

    const tbody = document.getElementById("tree-grid-body");
    if (tbody) {
        tbody.innerHTML = `
            <tr>
                <td colspan="100" class="grid-placeholder">
                    <div style="text-align: center; padding: 40px 20px;">
                        <i class="fa-solid fa-cloud-arrow-up" style="font-size: 48px; margin-bottom: 16px; color: var(--primary); opacity: 0.8;"></i>
                        <h3 style="font-size: 18px; margin-bottom: 8px; font-weight: 600; color: var(--text-base);">분석할 XBRL 패키지가 없습니다</h3>
                        <p style="color: var(--text-muted); font-size: 14px; margin-bottom: 24px;">금융감독원(DART) 등에서 내려받은 원문 XBRL ZIP 패키지를 업로드해 주세요.</p>
                        <button class="btn btn-primary" onclick="openUploadModal()">
                            <i class="fa-solid fa-cloud-arrow-up"></i> 파일 업로드하기
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }

    const flatTbody = document.getElementById("flat-grid-body");
    if (flatTbody) {
        flatTbody.innerHTML = `
            <tr>
                <td colspan="100" class="grid-placeholder">
                    파일 로드 대기 중...
                </td>
            </tr>
        `;
    }

    // 사이드바 목록 비우기
    const primaryContainer = document.getElementById("primary-sheets");
    const noteContainer = document.getElementById("note-sheets");
    if (primaryContainer) primaryContainer.innerHTML = "";
    if (noteContainer) noteContainer.innerHTML = "";
}

/**
 * 보고서 시트 목록을 API에서 불러와 사이드바에 렌더링합니다.
 * 기본적으로 재무상태표(D210000)를 자동 선택합니다.
 */
async function loadReportList() {
    try {
        const res = await fetch(`${BASE_URL}/api/reports`);
        if (!res.ok) throw new Error("보고서 목록 로드 실패");
        const list = await res.json();

        appState.reports = list;

        // 연결/별도 리포트 존재 여부에 따라 초기 reportType 결정
        const hasConsolidated = list.some((r) => {
            const label = r.label.toLowerCase();
            return label.includes("연결") || label.includes("consolidated");
        });
        const hasSeparate = list.some((r) => {
            const label = r.label.toLowerCase();
            return label.includes("별도") || label.includes("separate");
        });

        if (hasConsolidated) {
            appState.reportType = 'consolidated';
        } else if (hasSeparate) {
            appState.reportType = 'separate';
        } else {
            appState.reportType = 'consolidated';
        }

        // 토글 스위치 UI 활성화 상태 설정 및 비활성화 처리
        const btnConsolidated = document.querySelector(".toggle-segment[data-type='consolidated']");
        const btnSeparate = document.querySelector(".toggle-segment[data-type='separate']");

        if (btnConsolidated) {
            btnConsolidated.disabled = !hasConsolidated;
            btnConsolidated.style.opacity = hasConsolidated ? '1' : '0.5';
            btnConsolidated.style.cursor = hasConsolidated ? 'pointer' : 'not-allowed';
            if (!hasConsolidated) {
                btnConsolidated.title = '연결재무제표가 존재하지 않습니다.';
            } else {
                btnConsolidated.title = '';
            }
        }

        if (btnSeparate) {
            btnSeparate.disabled = !hasSeparate;
            btnSeparate.style.opacity = hasSeparate ? '1' : '0.5';
            btnSeparate.style.cursor = hasSeparate ? 'pointer' : 'not-allowed';
            if (!hasSeparate) {
                btnSeparate.title = '별도재무제표가 존재하지 않습니다.';
            } else {
                btnSeparate.title = '';
            }
        }

        document.querySelectorAll(".toggle-segment").forEach(btn => {
            btn.classList.toggle("active", btn.getAttribute("data-type") === appState.reportType);
        });

        renderReportList();

        // 재무상태표를 기본 선택
        if (list.length > 0) {
            const candidates = list.filter((r) => {
                const rLabel = r.label.toLowerCase();
                const rHasCons = rLabel.includes("연결") || rLabel.includes("consolidated");
                const rHasSep = rLabel.includes("별도") || rLabel.includes("separate");
                if (appState.reportType === 'consolidated') {
                    return !(rHasSep && !rHasCons);
                } else {
                    return !(rHasCons && !rHasSep);
                }
            });

            let defaultSheet = null;
            if (appState.reportType === 'consolidated') {
                defaultSheet = candidates.find((s) => s.role_uri.includes("D210000")) || candidates[0];
            } else {
                defaultSheet = candidates.find((s) => s.role_uri.includes("D210005")) || candidates[0];
            }

            if (!defaultSheet && list.length > 0) {
                defaultSheet = list[0];
            }

            if (defaultSheet) {
                selectReport(defaultSheet.role_uri);
            }
        }
    } catch (e) {
        console.error("보고서 목록 로드 오류:", e);
    }
}

/**
 * 특정 보고서 시트를 선택하여 데이터를 로드하고 그리드를 렌더링합니다.
 * @param {string} role_uri - 선택할 역할 URI
 */
async function selectReport(role_uri, active_axis = null) {
    appState.activeReportUri = role_uri;
    if (active_axis === null) {
        appState.collapsedNodes.clear();    // 접기 상태 초기화
        appState.selectedConceptId = null;  // 행 선택 초기화
        appState.parentMap = null;          // parent 맵 초기화
    }

    // 사이드바에서 활성 시트 강조
    document.querySelectorAll(".sheet-item").forEach((item) => {
        item.classList.toggle("active", item.getAttribute("data-uri") === role_uri);
    });

    // 로딩 표시
    document.getElementById("tree-grid-body").innerHTML = `
        <tr>
            <td colspan="100" class="grid-placeholder">
                <i class="fa-solid fa-circle-notch fa-spin"></i> 데이터를 분석 중입니다...
            </td>
        </tr>
    `;

    try {
        let url = `${BASE_URL}/api/report?role_uri=${encodeURIComponent(role_uri)}`;
        if (active_axis) {
            url += `&active_axis=${encodeURIComponent(active_axis)}`;
        }
        const res = await fetch(url);
        const report = await res.json();

        appState.activeReportData = report;

        // 헤더 제목 업데이트
        const sheet = appState.reports.find((r) => r.role_uri === role_uri);
        let sheetTitle = "Report Table";
        if (sheet) {
            // '[D210000] 재무상태표' → '재무상태표'
            const parts = sheet.label.split("]");
            sheetTitle = parts.length > 1 ? parts.pop().trim() : sheet.label;
        }
        document.getElementById("active-sheet-title").textContent = sheetTitle;
        document.getElementById("active-sheet-subtitle").textContent = role_uri;

        // 테이블 헤더에 동적 기간 표시 및 차원 컨트롤 보이기/숨기기
        const dimControls = document.getElementById("dimensional-controls");
        if (report.is_equity_statement) {
            if (dimControls) dimControls.style.display = "none";
            updateEquityTableHeaders(report.equity_columns);
        } else if (report.is_dimensional) {
            if (dimControls) dimControls.style.display = "flex";
            
            // Populate axis select dropdown
            const axisSelect = document.getElementById("axis-select");
            if (axisSelect) {
                axisSelect.innerHTML = report.dimensional_axes.map(ax => `<option value="${ax.axis}">${ax.axis_label}</option>`).join("");
                
                // Set active axis value
                const currentAxis = report.dimensional_columns[0] ? report.dimensional_columns[0].axis : report.dimensional_axes[0].axis;
                axisSelect.value = currentAxis;
                appState.activeAxis = currentAxis;
            }

            if (appState.pivotSwapped) {
                updatePivotedTableHeaders(report);
            } else {
                updateDimensionalTableHeaders(report.dimensional_columns);
            }
        } else {
            if (dimControls) dimControls.style.display = "none";
            restoreDefaultTableHeaders();
            updateTableHeaders(report.periods);
        }

        // parent 맵 빌드 (isAncestorCollapsed 최적화용)
        buildParentMap(report.data);

        // 그리드 및 대시보드 렌더링
        renderActiveReport();
        calculateDashboardRatios();
    } catch (e) {
        console.error("보고서 데이터 로드 오류:", e);
        document.getElementById("tree-grid-body").innerHTML = `
            <tr>
                <td colspan="100" class="grid-placeholder text-danger">
                    <i class="fa-solid fa-triangle-exclamation"></i>
                    데이터를 불러오지 못했습니다.
                </td>
            </tr>
        `;
    }
}

/**
 * 테이블 헤더에 보고 기간 정보를 동적으로 표시합니다.
 * @param {string[]} periods - 기간 문자열 배열 (예: ['2025-12-31', '2024-12-31'])
 */
function updateTableHeaders(periods) {
    const t_str = periods[0] || "당기";
    const t1_str = periods[1] || "전기";

    if (appState.reportType === 'consolidated') {
        const colConT = document.getElementById("col-con-t");
        const colConT1 = document.getElementById("col-con-t1");
        const flatColConT = document.getElementById("flat-col-con-t");
        const flatColConT1 = document.getElementById("flat-col-con-t1");

        if (colConT) colConT.innerHTML = `연결 당기<br><small>${t_str}</small>`;
        if (colConT1) colConT1.innerHTML = `연결 전기<br><small>${t1_str}</small>`;
        if (flatColConT) flatColConT.textContent = `연결 당기 (${t_str})`;
        if (flatColConT1) flatColConT1.textContent = `연결 전기 (${t1_str})`;
    } else {
        const colSepT = document.getElementById("col-sep-t");
        const colSepT1 = document.getElementById("col-sep-t1");
        const flatColSepT = document.getElementById("flat-col-sep-t");
        const flatColSepT1 = document.getElementById("flat-col-sep-t1");

        if (colSepT) colSepT.innerHTML = `별도 당기<br><small>${t_str}</small>`;
        if (colSepT1) colSepT1.innerHTML = `별도 전기<br><small>${t1_str}</small>`;
        if (flatColSepT) flatColSepT.textContent = `별도 당기 (${t_str})`;
        if (flatColSepT1) flatColSepT1.textContent = `별도 전기 (${t1_str})`;
    }
}

/**
 * 자본변동표의 dynamic columns에 맞춰 테이블 헤더를 생성합니다.
 * @param {Object[]} equityColumns - dynamic columns 메타데이터
 */
function updateEquityTableHeaders(equityColumns) {
    // Tree Grid Header (단일 행 헤더)
    const treeGrid = document.getElementById("xbrl-tree-grid");
    const treeHead = treeGrid.querySelector("thead");
    
    let treeHeadHtml = `
        <tr>
            <th class="col-label">계정과목 (Account Element)</th>
    `;
    equityColumns.forEach(col => {
        const periodLabel = col.period === 't' ? '당기' : (col.period === 't1' ? '전기' : '전전기');
        treeHeadHtml += `<th class="col-val" style="text-align: right; min-width: 130px; font-size: 11px;">${col.label}<br><small>${periodLabel} ${col.period_str}</small></th>`;
    });
    treeHeadHtml += `</tr>`;
    treeHead.innerHTML = treeHeadHtml;

    // Flat Grid Header
    const flatGrid = document.getElementById("xbrl-flat-grid");
    const flatHead = flatGrid.querySelector("thead");
    
    let flatHeadHtml = `
        <tr>
            <th>Parent ID</th>
            <th>Label (KO)</th>
            <th>Depth</th>
            <th>Abstract</th>
    `;
    equityColumns.forEach(col => {
        const periodLabel = col.period === 't' ? '당기' : (col.period === 't1' ? '전기' : '전전기');
        flatHeadHtml += `<th style="text-align: right; min-width: 130px; font-size: 11px;">${col.label} (${periodLabel})</th>`;
    });
    flatHeadHtml += `</tr>`;
    flatHead.innerHTML = flatHeadHtml;
}

/**
 * 차원 테이블의 dynamic columns에 맞춰 테이블 헤더를 생성합니다.
 * @param {Object[]} columns - dynamic columns 메타데이터
 */
function updateDimensionalTableHeaders(columns) {
    // Tree Grid Header (단일 행 헤더)
    const treeGrid = document.getElementById("xbrl-tree-grid");
    const treeHead = treeGrid.querySelector("thead");
    
    let treeHeadHtml = `
        <tr>
            <th class="col-label">계정과목 (Account Element)</th>
    `;
    columns.forEach(col => {
        const periodLabel = col.period === 't' ? '당기' : (col.period === 't1' ? '전기' : '전전기');
        treeHeadHtml += `<th class="col-val" style="text-align: right; min-width: 130px; font-size: 11px;">${col.label}<br><small>${periodLabel} ${col.period_str}</small></th>`;
    });
    treeHeadHtml += `</tr>`;
    treeHead.innerHTML = treeHeadHtml;

    // Flat Grid Header
    const flatGrid = document.getElementById("xbrl-flat-grid");
    const flatHead = flatGrid.querySelector("thead");
    
    let flatHeadHtml = `
        <tr>
            <th>Parent ID</th>
            <th>Label (KO)</th>
            <th>Depth</th>
            <th>Abstract</th>
    `;
    columns.forEach(col => {
        const periodLabel = col.period === 't' ? '당기' : (col.period === 't1' ? '전기' : '전전기');
        flatHeadHtml += `<th style="text-align: right; min-width: 130px; font-size: 11px;">${col.label} (${periodLabel})</th>`;
    });
    flatHeadHtml += `</tr>`;
    flatHead.innerHTML = flatHeadHtml;
}

/**
 * 피벗(축 전환)된 차원 테이블의 dynamic columns에 맞춰 테이블 헤더를 생성합니다.
 */
function updatePivotedTableHeaders(report) {
    const pivotedColumns = getPivotedColumns(report);
    
    // Tree Grid Header
    const treeGrid = document.getElementById("xbrl-tree-grid");
    const treeHead = treeGrid.querySelector("thead");
    
    let treeHeadHtml = `
        <tr>
            <th class="col-label">차원 멤버 (Dimension Member)</th>
    `;
    pivotedColumns.forEach(col => {
        const periodLabel = col.period === 't' ? '당기' : (col.period === 't1' ? '전기' : '전전기');
        treeHeadHtml += `<th class="col-val" style="text-align: right; min-width: 140px; font-size: 11px;">${col.label}<br><small>${periodLabel} ${col.period_str}</small></th>`;
    });
    treeHeadHtml += `</tr>`;
    treeHead.innerHTML = treeHeadHtml;

    // Flat Grid Header
    const flatGrid = document.getElementById("xbrl-flat-grid");
    const flatHead = flatGrid.querySelector("thead");
    
    let flatHeadHtml = `
        <tr>
            <th>Parent ID</th>
            <th>Member Label (KO)</th>
            <th>Depth</th>
            <th>Abstract</th>
    `;
    pivotedColumns.forEach(col => {
        const periodLabel = col.period === 't' ? '당기' : (col.period === 't1' ? '전기' : '전전기');
        flatHeadHtml += `<th style="text-align: right; min-width: 140px; font-size: 11px;">${col.label} (${periodLabel})</th>`;
    });
    flatHeadHtml += `</tr>`;
    flatHead.innerHTML = flatHeadHtml;
}

/**
 * 피벗 뷰에서 사용할 컬럼 목록을 생성합니다.
 */
function getPivotedColumns(report) {
    const lineItems = report.data.filter(row => !row.is_abstract);
    const pivotedColumns = [];
    lineItems.forEach(item => {
        report.periods.forEach((p_str, idx) => {
            const periodKey = ['t', 't1', 't2'][idx];
            pivotedColumns.push({
                key: `${item.concept_id}_${periodKey}`,
                label: item.label_ko,
                concept_id: item.concept_id,
                period: periodKey,
                period_str: p_str
            });
        });
    });
    return pivotedColumns;
}

/**
 * 차원 멤버에 대한 접힌 조상 노드가 있는지 확인합니다.
 */
function isMemberAncestorCollapsed(parent_id, collapsedSet, parentMap) {
    if (!parent_id) return false;
    if (collapsedSet.has(parent_id)) return true;
    const grandParent = parentMap.get(parent_id);
    return isMemberAncestorCollapsed(grandParent, collapsedSet, parentMap);
}

/**
 * 테이블 헤더를 원래 상태(연결/별도 당기/전기)로 복구합니다.
 */
function restoreDefaultTableHeaders() {
    // Rebuild tree grid headers
    const treeGrid = document.getElementById("xbrl-tree-grid");
    const treeHead = treeGrid.querySelector("thead");
    const flatGrid = document.getElementById("xbrl-flat-grid");
    const flatHead = flatGrid.querySelector("thead");

    if (appState.reportType === 'consolidated') {
        treeHead.innerHTML = `
            <tr>
                <th class="col-label">계정과목 (Account Element)</th>
                <th class="col-val" id="col-con-t">연결 당기</th>
                <th class="col-val" id="col-con-t1">연결 전기</th>
            </tr>
        `;
        flatHead.innerHTML = `
            <tr>
                <th>Parent ID</th>
                <th>Label (KO)</th>
                <th>Depth</th>
                <th>Abstract</th>
                <th id="flat-col-con-t">Cons. Current</th>
                <th id="flat-col-con-t1">Cons. Prior</th>
            </tr>
        `;
    } else {
        treeHead.innerHTML = `
            <tr>
                <th class="col-label">계정과목 (Account Element)</th>
                <th class="col-val" id="col-sep-t">별도 당기</th>
                <th class="col-val" id="col-sep-t1">별도 전기</th>
            </tr>
        `;
        flatHead.innerHTML = `
            <tr>
                <th>Parent ID</th>
                <th>Label (KO)</th>
                <th>Depth</th>
                <th>Abstract</th>
                <th id="flat-col-sep-t">Sep. Current</th>
                <th id="flat-col-sep-t1">Sep. Prior</th>
            </tr>
        `;
    }
}

// ==========================================================================
// 사이드바 — 보고서 시트 목록 렌더링
// ==========================================================================

/** 보고서 시트 목록을 사이드바에 렌더링합니다 (검색 필터 적용). */
function renderReportList() {
    const primaryContainer = document.getElementById("primary-sheets");
    const noteContainer = document.getElementById("note-sheets");

    primaryContainer.innerHTML = "";
    noteContainer.innerHTML = "";

    const query = appState.sheetSearchQuery.toLowerCase();

    const primaryItems = [];
    const noteItems = [];

    appState.reports.forEach((r) => {
        // 검색 필터 적용
        if (query && !r.label.toLowerCase().includes(query)) return;

        // 연결/별도 필터 적용
        const label = r.label.toLowerCase();
        let hasCons = label.includes("연결") || label.includes("consolidated");
        let hasSep = label.includes("별도") || label.includes("separate");

        // DART 코드 규칙 적용 (마지막 자리가 0이면 연결, 5이면 별도)
        const matches = r.label.match(/\[(.*?)\]\s*(.*)/);
        const code = matches ? matches[1].trim() : "";
        if (code) {
            const lastChar = code.charAt(code.length - 1);
            if (lastChar === '0') {
                hasCons = true;
            } else if (lastChar === '5') {
                hasSep = true;
            }
        }

        if (appState.reportType === 'consolidated') {
            if (hasSep && !hasCons) return;
        } else if (appState.reportType === 'separate') {
            if (hasCons && !hasSep) return;
        }

        // 주요 재무제표 vs 주석 그룹 분류 (D2~D6으로 시작하면 주요 재무제표)
        const isPrimary =
            code.startsWith("D2") || code.startsWith("D3") || code.startsWith("D4") ||
            code.startsWith("D5") || code.startsWith("D6");

        if (isPrimary) {
            primaryItems.push(r);
        } else {
            noteItems.push(r);
        }
    });

    // 주석 항목들은 번호순으로 정렬
    noteItems.sort((a, b) => {
        const getNoteNumber = (lbl) => {
            const match = lbl.match(/\]\s*(\d+)\./);
            if (match) {
                return parseInt(match[1], 10);
            }
            const matchNoBracket = lbl.match(/^\s*(\d+)\./);
            if (matchNoBracket) {
                return parseInt(matchNoBracket[1], 10);
            }
            return null;
        };

        const numA = getNoteNumber(a.label);
        const numB = getNoteNumber(b.label);

        if (numA !== null && numB !== null) {
            if (numA !== numB) {
                return numA - numB;
            }
        } else if (numA !== null) {
            return -1;
        } else if (numB !== null) {
            return 1;
        }
        return a.label.localeCompare(b.label);
    });

    const createSheetLi = (r) => {
        const li = document.createElement("li");
        li.className = "sheet-item";
        li.setAttribute("data-uri", r.role_uri);

        // 레이블 정리: '[D210000] 재무상태표' → 코드 + 이름
        const codeMatch = r.label.match(/\[(.*?)\]\s*(.*)/);
        let cleanName = codeMatch ? codeMatch[2].split("|")[0].trim() : r.label;

        // 시트 유형별 아이콘 선택
        let iconClass = "fa-solid fa-file-invoice";
        if (cleanName.includes("재무상태")) iconClass = "fa-solid fa-balance-scale";
        else if (cleanName.includes("손익계산")) iconClass = "fa-solid fa-arrow-trend-up";
        else if (cleanName.includes("포괄손익")) iconClass = "fa-solid fa-money-bill-trend-up";
        else if (cleanName.includes("현금흐름")) iconClass = "fa-solid fa-money-bill-wave";
        else if (cleanName.includes("자본변동")) iconClass = "fa-solid fa-users-viewfinder";

        li.innerHTML = `<i class="${iconClass}"></i> <span class="label-txt">${cleanName}</span>`;
        li.addEventListener("click", () => selectReport(r.role_uri));
        return li;
    };

    primaryItems.forEach((r) => {
        primaryContainer.appendChild(createSheetLi(r));
    });

    noteItems.forEach((r) => {
        noteContainer.appendChild(createSheetLi(r));
    });
}

// ==========================================================================
// 트리 그리드 & 플랫 테이블 렌더링
// ==========================================================================

/**
 * concept_id → parent_id 맵을 빌드합니다.
 * isAncestorCollapsed에서 재귀적 data.find() 대신 O(1) 조회를 사용하기 위한 최적화.
 * @param {Object[]} data - 보고서 데이터 배열
 */
function buildParentMap(data) {
    const map = new Map();
    data.forEach((row) => {
        map.set(row.concept_id, row.parent_id);
    });
    appState.parentMap = map;
}

/** 현재 활성 보고서의 트리 뷰와 플랫 뷰를 모두 렌더링합니다. */
function renderActiveReport() {
    if (!appState.activeReportData) return;
    renderTreeGrid();
    renderFlatGrid();
}

/**
 * 계층 트리 그리드를 렌더링합니다.
 * 접힌 노드의 하위 항목은 표시하지 않으며, 검색어 필터링과 하이라이트를 지원합니다.
 */
function renderTreeGrid() {
    const tbody = document.getElementById("tree-grid-body");
    tbody.innerHTML = "";

    const report = appState.activeReportData;
    if (!report || !report.data || report.data.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="100" class="grid-placeholder">
                    이 보고서에 저장된 데이터가 없습니다.
                </td>
            </tr>
        `;
        return;
    }

    const data = report.data;

    // ── 다축 주석 Pivoted 렌더링 분기 ──
    if (report.is_dimensional && appState.pivotSwapped) {
        const axisObj = report.dimensional_axes.find(a => a.axis === appState.activeAxis);
        const axisMembers = axisObj ? axisObj.members : [];
        const pivotedColumns = getPivotedColumns(report);
        
        const memberParentSet = new Set(axisMembers.map(m => m.parent_id).filter(p => p !== null));
        const memberParentMap = new Map();
        axisMembers.forEach(m => {
            memberParentMap.set(m.id, m.parent_id);
        });

        axisMembers.forEach(member => {
            // 접힌 조상 노드 확인
            if (isMemberAncestorCollapsed(member.parent_id, appState.collapsedNodes, memberParentMap)) return;

            const tr = document.createElement("tr");
            tr.className = `row-depth-${member.depth}`;
            if (member.id === appState.selectedConceptId) tr.classList.add("highlight-row");
            tr.setAttribute("data-id", member.id);

            // 1열: 차원 멤버명 (계층구조 + 접기/펼치기)
            const tdLabel = document.createElement("td");
            tdLabel.className = "col-label";

            const indentWidth = member.depth * 24;
            const hasChildren = memberParentSet.has(member.id);
            const isCollapsed = appState.collapsedNodes.has(member.id);

            let toggleIcon = `<span class="tree-toggle hidden"></span>`;
            if (hasChildren) {
                toggleIcon = `<span class="tree-toggle ${isCollapsed ? 'collapsed' : ''}" data-action="toggle" data-id="${member.id}"><i class="fa-solid fa-chevron-down"></i></span>`;
            }

            tdLabel.innerHTML = `
                <div class="tree-node-label" style="padding-left: ${indentWidth}px">
                    ${toggleIcon}
                    <span class="concept-label-tooltip" data-tooltip="${member.id}">${member.label}</span>
                </div>
            `;

            if (hasChildren) {
                tdLabel.querySelector(".tree-toggle").addEventListener("click", (e) => {
                    e.stopPropagation();
                    toggleNode(member.id);
                });
            }
            tr.appendChild(tdLabel);

            // value 열들 (Line Items × Periods)
            pivotedColumns.forEach(col => {
                const tdVal = document.createElement("td");
                tdVal.className = "col-val";

                const origRow = data.find(r => r.concept_id === col.concept_id);
                const rawVal = origRow ? origRow[`${member.id}_${col.period}`] : null;

                if (rawVal === null || rawVal === undefined) {
                    tdVal.innerHTML = `<span class="text-muted">-</span>`;
                } else if (typeof rawVal === 'string') {
                    tdVal.textContent = rawVal;
                } else {
                    const scaledVal = rawVal / appState.currentScale;
                    tdVal.textContent = formatNumber(scaledVal);
                }
                tr.appendChild(tdVal);
            });

            tr.addEventListener("click", () => selectRow(member.id));
            tbody.appendChild(tr);
        });
        return;
    }

    // ── 일반적인 트리 렌더링 (Normal 주석, 자본변동표, 일반 재무제표) ──
    const parentSet = new Set(
        data.map((r) => r.parent_id).filter((p) => p !== null)
    );

    const query = appState.searchQuery.toLowerCase();

    data.forEach((row) => {
        if (isAncestorCollapsed(row.parent_id)) return;

        if (query) {
            const labelMatch =
                row.label_ko.toLowerCase().includes(query) ||
                row.label_en.toLowerCase().includes(query);
            const idMatch = row.concept_id.toLowerCase().includes(query);
            if (!labelMatch && !idMatch) return;
        }

        const tr = document.createElement("tr");
        tr.className = `row-depth-${row.depth}`;
        if (row.is_abstract) tr.classList.add("row-abstract");
        if (row.concept_id === appState.selectedConceptId) tr.classList.add("highlight-row");
        tr.setAttribute("data-id", row.concept_id);

        const tdLabel = document.createElement("td");
        tdLabel.className = "col-label";

        const indentWidth = row.depth * 24;
        const hasChildren = parentSet.has(row.concept_id);
        const isCollapsed = appState.collapsedNodes.has(row.concept_id);

        let toggleIcon = `<span class="tree-toggle hidden"></span>`;
        if (hasChildren) {
            toggleIcon = `<span class="tree-toggle ${isCollapsed ? 'collapsed' : ''}" data-action="toggle" data-id="${row.concept_id}"><i class="fa-solid fa-chevron-down"></i></span>`;
        }

        let labelText = row.label_ko;
        if (query) {
            const idx = labelText.toLowerCase().indexOf(query);
            if (idx >= 0) {
                labelText =
                    labelText.substring(0, idx) +
                    `<span class="highlight-match">${labelText.substring(idx, idx + query.length)}</span>` +
                    labelText.substring(idx + query.length);
            }
        }

        tdLabel.innerHTML = `
            <div class="tree-node-label" style="padding-left: ${indentWidth}px">
                ${toggleIcon}
                <span class="concept-label-tooltip" data-tooltip="${row.concept_id}">${labelText}</span>
            </div>
        `;

        if (hasChildren) {
            tdLabel.querySelector(".tree-toggle").addEventListener("click", (e) => {
                e.stopPropagation();
                toggleNode(row.concept_id);
            });
        }
        tr.appendChild(tdLabel);

        // value 열들
        if (report.is_equity_statement) {
            report.equity_columns.forEach((col) => {
                const tdVal = document.createElement("td");
                tdVal.className = "col-val";

                const rawVal = row[col.key];
                if (row.is_abstract || rawVal === null || rawVal === undefined) {
                    tdVal.innerHTML = `<span class="text-muted">-</span>`;
                } else if (typeof rawVal === 'string') {
                    tdVal.textContent = rawVal;
                } else {
                    const scaledVal = rawVal / appState.currentScale;
                    tdVal.textContent = formatNumber(scaledVal);
                }
                tr.appendChild(tdVal);
            });
        } else if (report.is_dimensional) {
            report.dimensional_columns.forEach((col) => {
                const tdVal = document.createElement("td");
                tdVal.className = "col-val";

                const rawVal = row[col.key];
                if (row.is_abstract || rawVal === null || rawVal === undefined) {
                    tdVal.innerHTML = `<span class="text-muted">-</span>`;
                } else if (typeof rawVal === 'string') {
                    tdVal.textContent = rawVal;
                } else {
                    const scaledVal = rawVal / appState.currentScale;
                    tdVal.textContent = formatNumber(scaledVal);
                }
                tr.appendChild(tdVal);
            });
        } else {
            const valKeys = appState.reportType === 'consolidated'
                ? ['consolidated_t', 'consolidated_t1']
                : ['separate_t', 'separate_t1'];
            valKeys.forEach((k) => {
                const tdVal = document.createElement("td");
                tdVal.className = "col-val";

                const rawVal = row[k];
                if (row.is_abstract || rawVal === null || rawVal === undefined) {
                    tdVal.innerHTML = `<span class="text-muted">-</span>`;
                } else if (typeof rawVal === 'string') {
                    tdVal.textContent = rawVal;
                } else {
                    const scaledVal = rawVal / appState.currentScale;
                    tdVal.textContent = formatNumber(scaledVal);
                }
                tr.appendChild(tdVal);
            });
        }

        tr.addEventListener("click", () => selectRow(row.concept_id));
        tbody.appendChild(tr);
    });
}

/** 2차원 플랫 DataFrame 뷰를 렌더링합니다. */
function renderFlatGrid() {
    const tbody = document.getElementById("flat-grid-body");
    tbody.innerHTML = "";

    const report = appState.activeReportData;
    if (!report || !report.data || report.data.length === 0) return;

    if (report.is_dimensional && appState.pivotSwapped) {
        const axisObj = report.dimensional_axes.find(a => a.axis === appState.activeAxis);
        const axisMembers = axisObj ? axisObj.members : [];
        const pivotedColumns = getPivotedColumns(report);

        axisMembers.forEach(member => {
            const tr = document.createElement("tr");
            if (member.id === appState.selectedConceptId) tr.classList.add("highlight-row");

            let html = `
                <td style="font-family: monospace; font-size:11px; color:var(--text-muted);">${member.parent_id || "-"}</td>
                <td><strong class="concept-label-tooltip" data-tooltip="${member.id}">${member.label}</strong></td>
                <td align="center">${member.depth}</td>
                <td align="center">-</td>
            `;

            pivotedColumns.forEach(col => {
                const origRow = report.data.find(r => r.concept_id === col.concept_id);
                const rawVal = origRow ? origRow[`${member.id}_${col.period}`] : null;
                html += `<td align="right">${formatRaw(rawVal)}</td>`;
            });

            tr.innerHTML = html;
            tbody.appendChild(tr);
        });
        return;
    }

    report.data.forEach((row) => {
        const tr = document.createElement("tr");
        if (row.concept_id === appState.selectedConceptId) tr.classList.add("highlight-row");

        let html = `
            <td style="font-family: monospace; font-size:11px; color:var(--text-muted);">${row.parent_id || "-"}</td>
            <td><strong class="concept-label-tooltip" data-tooltip="${row.concept_id}">${row.label_ko}</strong></td>
            <td align="center">${row.depth}</td>
            <td align="center">${row.is_abstract ? '<i class="fa-solid fa-check text-purple"></i>' : '-'}</td>
        `;

        if (report.is_equity_statement) {
            report.equity_columns.forEach((col) => {
                html += `<td align="right">${formatRaw(row[col.key])}</td>`;
            });
        } else if (report.is_dimensional) {
            report.dimensional_columns.forEach((col) => {
                html += `<td align="right">${formatRaw(row[col.key])}</td>`;
            });
        } else {
            if (appState.reportType === 'consolidated') {
                html += `
                    <td align="right">${formatRaw(row.consolidated_t)}</td>
                    <td align="right">${formatRaw(row.consolidated_t1)}</td>
                `;
            } else {
                html += `
                    <td align="right">${formatRaw(row.separate_t)}</td>
                    <td align="right">${formatRaw(row.separate_t1)}</td>
                `;
            }
        }

        tr.innerHTML = html;
        tbody.appendChild(tr);
    });
}

// ==========================================================================
// 트리 노드 조작 (접기/펼치기, 선택)
// ==========================================================================

/**
 * 조상 노드 중 접힌 노드가 있는지 확인합니다.
 * parentMap을 사용하여 O(depth) 시간에 판별합니다.
 *
 * @param {string|null} parent_id - 확인할 부모 concept_id
 * @returns {boolean} 조상 중 접힌 노드가 있으면 true
 */
function isAncestorCollapsed(parent_id) {
    if (!parent_id) return false;
    if (appState.collapsedNodes.has(parent_id)) return true;

    // parentMap으로 빠르게 상위 순회
    if (appState.parentMap) {
        const grandParent = appState.parentMap.get(parent_id);
        return isAncestorCollapsed(grandParent);
    }
    return false;
}

/**
 * 노드의 접기/펼치기 상태를 전환하고 트리를 다시 렌더링합니다.
 * @param {string} nodeId - 토글할 노드의 concept_id
 */
function toggleNode(nodeId) {
    if (appState.collapsedNodes.has(nodeId)) {
        appState.collapsedNodes.delete(nodeId);
    } else {
        appState.collapsedNodes.add(nodeId);
    }
    renderTreeGrid();
}

/**
 * 트리에서 행을 선택합니다 (수식에 Concept ID를 삽입할 때 사용).
 * @param {string} conceptId - 선택할 concept_id
 */
function selectRow(conceptId) {
    appState.selectedConceptId = conceptId;

    // 선택된 행 시각적 강조
    document.querySelectorAll("#tree-grid-body tr").forEach((tr) => {
        tr.classList.toggle("highlight-row", tr.getAttribute("data-id") === conceptId);
    });
}

// ==========================================================================
// 숫자 포매팅 유틸리티
// ==========================================================================

/**
 * 스케일이 적용된 숫자를 가독성 있게 포매팅합니다.
 * @param {number} num - 포매팅할 숫자
 * @returns {string} 포매팅된 문자열
 */
function formatNumber(num) {
    if (Math.abs(num) < 0.001 && num !== 0) {
        return num.toFixed(4);
    }
    return num.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 1,
    });
}

/**
 * 원시 팩트 값을 로케일 포맷으로 변환합니다 (플랫 뷰용).
 * @param {*} val - 원시 값
 * @returns {string} 포매팅된 문자열 또는 '-'
 */
function formatRaw(val) {
    if (val === null || val === undefined) return "-";
    if (typeof val === 'string') return val;
    return val.toLocaleString();
}

// ==========================================================================
// 재무 분석 패널 — 대시보드 비율 + 사용자 정의 수식
// ==========================================================================

/**
 * 대시보드의 사전 정의된 재무비율(유동비율, 부채비율, 영업이익률)을
 * API를 통해 계산하고 카드에 표시합니다.
 */
async function calculateDashboardRatios() {
    try {
        const body = {
            formulas: {
                curr_con: PREDEFINED_RATIOS.current_ratio.expr,
                debt_con: PREDEFINED_RATIOS.debt_ratio.expr,
                op_con: PREDEFINED_RATIOS.op_margin.expr,
            },
        };

        const res = await fetch(`${BASE_URL}/api/analyze`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });

        if (!res.ok) throw new Error("비율 계산 API 오류");
        const data = await res.json();

        // 비율을 퍼센트(%)로 변환하는 헬퍼
        const renderRatio = (val) =>
            val !== null && val !== undefined ? `${(val * 100).toFixed(1)}%` : "-";

        // 유동비율 카드 업데이트
        document.getElementById("ratio-curr-con").textContent =
            renderRatio(data.curr_con.consolidated_t);
        document.getElementById("ratio-prev-con").textContent =
            renderRatio(data.curr_con.consolidated_t1);

        // 부채비율 카드 업데이트
        document.getElementById("ratio-debt-curr").textContent =
            renderRatio(data.debt_con.consolidated_t);
        document.getElementById("ratio-debt-prev").textContent =
            renderRatio(data.debt_con.consolidated_t1);

        // 영업이익률 카드 업데이트
        document.getElementById("ratio-op-curr").textContent =
            renderRatio(data.op_con.consolidated_t);
        document.getElementById("ratio-op-prev").textContent =
            renderRatio(data.op_con.consolidated_t1);
    } catch (e) {
        console.error("대시보드 비율 계산 실패:", e);
    }
}

/**
 * 사용자가 입력한 수식을 백엔드 API를 통해 평가하고 결과를 표시합니다.
 * 연산 결과와 함께 사용된 계정값의 감사 추적(audit trail)도 보여줍니다.
 */
async function runCustomCalculation() {
    const name =
        document.getElementById("custom-formula-name").value.trim() || "사용자 정의 수식";
    const expr = document.getElementById("custom-formula-expr").value.trim();

    if (!expr) {
        alert("수식을 입력해 주세요.");
        return;
    }

    const resultsBox = document.getElementById("formula-results-box");
    resultsBox.style.display = "block";
    resultsBox.innerHTML = `
        <div class="grid-placeholder" style="padding: 16px !important;">
            <i class="fa-solid fa-circle-notch fa-spin"></i> 수식 연산 중...
        </div>
    `;

    try {
        const body = { formulas: { custom: expr } };

        const res = await fetch(`${BASE_URL}/api/analyze`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const error = await res.json();
            throw new Error(error.detail || "연산 오류");
        }

        const data = await res.json();
        const customRes = data.custom;

        // 결과 포매팅 헬퍼
        const formatResult = (val) => {
            if (val === null || val === undefined) return "-";
            return val.toLocaleString(undefined, { maximumFractionDigits: 2 });
        };

        // 감사 추적(audit trail) HTML 생성
        let auditListHtml = "";
        for (const [concept, info] of Object.entries(customRes.values_used)) {
            const val = appState.reportType === 'consolidated'
                ? (info.consolidated_t !== undefined ? formatRaw(info.consolidated_t) : "-")
                : (info.separate_t !== undefined ? formatRaw(info.separate_t) : "-");
            auditListHtml += `
                <li>
                    <strong>${info.label}</strong>
                    <span>(${concept}) = ${val}</span>
                </li>
            `;
        }

        let headerHtml = "";
        let bodyHtml = "";
        if (appState.reportType === 'consolidated') {
            headerHtml = `
                <tr>
                    <th>구분</th>
                    <th>연결 당기</th>
                    <th>연결 전기</th>
                </tr>
            `;
            bodyHtml = `
                <tr>
                    <td>연산값</td>
                    <td class="text-green">${formatResult(customRes.consolidated_t)}</td>
                    <td>${formatResult(customRes.consolidated_t1)}</td>
                </tr>
            `;
        } else {
            headerHtml = `
                <tr>
                    <th>구분</th>
                    <th>별도 당기</th>
                    <th>별도 전기</th>
                </tr>
            `;
            bodyHtml = `
                <tr>
                    <td>연산값</td>
                    <td class="text-blue">${formatResult(customRes.separate_t)}</td>
                    <td>${formatResult(customRes.separate_t1)}</td>
                </tr>
            `;
        }

        const activeTypeText = appState.reportType === 'consolidated' ? 'Consolidated Current' : 'Separate Current';

        resultsBox.innerHTML = `
            <h4>연산 결과: ${name}</h4>
            <div class="results-table-wrapper">
                <table class="results-table">
                    <thead>
                        ${headerHtml}
                    </thead>
                    <tbody>
                        ${bodyHtml}
                    </tbody>
                </table>
            </div>

            <div class="audit-trail">
                <h5>사용된 계정값 정보 (${activeTypeText}):</h5>
                <ul>
                    ${auditListHtml || '<li>사용된 계정 정보가 없습니다. 수식을 확인하세요.</li>'}
                </ul>
            </div>
        `;
    } catch (e) {
        resultsBox.innerHTML = `
            <div class="help-text" style="background-color: rgba(244, 63, 94, 0.1); border-left-color: var(--danger); color: var(--danger);">
                <h4>연산 에러</h4>
                <p>${e.message || "수식을 분석하거나 연산하는 데 실패했습니다. 수식 문법과 Concept ID를 확인해 주세요."}</p>
            </div>
        `;
    }
}

// ==========================================================================
// ZIP 파일 업로드 핸들러
// ==========================================================================

/** 업로드 모달을 엽니다. */
function openUploadModal() {
    document.getElementById("upload-modal").classList.add("show");
}

/** 업로드 모달을 닫습니다. */
function closeUploadModal() {
    document.getElementById("upload-modal").classList.remove("show");
    document.getElementById("upload-status").style.display = "none";
}

/**
 * ZIP 파일을 서버에 업로드하고 XBRL 데이터를 로드합니다.
 * @param {File} file - 업로드할 ZIP 파일 객체
 */
async function handleZipUpload(file) {
    const statusDiv = document.getElementById("upload-status");
    statusDiv.style.display = "block";
    statusDiv.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i> 파일을 업로드하고 분석하는 중입니다... (약 5~10초 소요)`;

    const formData = new FormData();
    formData.append("file", file);

    try {
        const res = await fetch(`${BASE_URL}/api/upload`, {
            method: "POST",
            body: formData,
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "업로드 실패");
        }

        // 성공
        statusDiv.innerHTML = `<i class="fa-solid fa-check text-green"></i> 업로드 및 분석 성공!`;
        setTimeout(() => {
            closeUploadModal();
            checkFileStatus();
        }, 1000);
    } catch (e) {
        statusDiv.innerHTML = `<i class="fa-solid fa-triangle-exclamation text-danger"></i> 분석 실패: ${e.message}`;
    }
}

// ==========================================================================
// 이벤트 리스너 등록
// ==========================================================================

/** 앱의 모든 이벤트 리스너를 등록합니다. */
function setupEventListeners() {
    // ── 테마 전환 ──
    document.getElementById("theme-toggle").addEventListener("click", toggleTheme);

    // ── 숫자 단위 스케일 변경 ──
    document.getElementById("number-scale").addEventListener("change", (e) => {
        appState.currentScale = parseFloat(e.target.value);
        renderTreeGrid();
    });

    // ── 사이드바 시트 검색 ──
    document.getElementById("sheet-search").addEventListener("input", (e) => {
        appState.sheetSearchQuery = e.target.value;
        renderReportList();
    });

    // ── 트리 내 검색 (필터링) ──
    document.getElementById("grid-search").addEventListener("input", (e) => {
        appState.searchQuery = e.target.value;
        renderTreeGrid();
    });

    // ── 탭 전환 (트리 뷰 / DataFrame / 분석) ──
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");

            // 활성 탭 버튼 갱신
            document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");

            // 활성 탭 패널 갱신
            document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
            document.getElementById(targetTab).classList.add("active");

            // 트리 뷰 탭에서만 검색창 표시
            const searchWrapper = document.getElementById("tree-search-wrapper");
            searchWrapper.style.display = targetTab === "tab-tree" ? "block" : "none";
        });
    });

    // ── 내보내기 드롭다운 ──
    const exportBtn = document.getElementById("btn-export-dropdown");
    const exportMenu = document.getElementById("export-menu");

    exportBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        exportMenu.classList.toggle("show");
    });

    document.addEventListener("click", () => {
        exportMenu.classList.remove("show");
    });

    // Excel 내보내기
    document.getElementById("export-excel").addEventListener("click", (e) => {
        e.preventDefault();
        if (appState.activeReportUri) {
            let url = `${BASE_URL}/api/export?role_uri=${encodeURIComponent(appState.activeReportUri)}&format=excel`;
            if (appState.activeAxis) {
                url += `&active_axis=${encodeURIComponent(appState.activeAxis)}`;
            }
            window.location.href = url;
        }
    });

    // CSV 내보내기
    document.getElementById("export-csv").addEventListener("click", (e) => {
        e.preventDefault();
        if (appState.activeReportUri) {
            let url = `${BASE_URL}/api/export?role_uri=${encodeURIComponent(appState.activeReportUri)}&format=csv`;
            if (appState.activeAxis) {
                url += `&active_axis=${encodeURIComponent(appState.activeAxis)}`;
            }
            window.location.href = url;
        }
    });

    // ── 수식 패널: 선택된 계정 삽입 ──
    document.getElementById("btn-insert-concept").addEventListener("click", () => {
        if (!appState.selectedConceptId) {
            alert("테이블에서 수식에 추가할 계정(행)을 선택해 주세요.");
            return;
        }
        const textarea = document.getElementById("custom-formula-expr");
        const cursor = textarea.selectionStart;
        const text = textarea.value;
        textarea.value =
            text.substring(0, cursor) + appState.selectedConceptId + text.substring(cursor);
        textarea.focus();
    });

    // ── 수식 패널: 수식 실행 ──
    document.getElementById("btn-calc-formula").addEventListener("click", runCustomCalculation);

    // ── 업로드 모달 ──
    document.getElementById("btn-upload-trigger").addEventListener("click", openUploadModal);
    document.getElementById("btn-close-modal").addEventListener("click", closeUploadModal);

    // ── ZIP 드래그앤드롭 업로드 ──
    const zone = document.getElementById("drag-drop-zone");

    zone.addEventListener("click", () => {
        document.getElementById("file-input").click();
    });

    document.getElementById("file-input").addEventListener("change", (e) => {
        if (e.target.files.length > 0) {
            handleZipUpload(e.target.files[0]);
        }
    });

    zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
    });

    zone.addEventListener("dragleave", () => {
        zone.classList.remove("dragover");
    });

    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
        if (e.dataTransfer.files.length > 0) {
            handleZipUpload(e.dataTransfer.files[0]);
        }
    });

    // ── 연결/별도 토글 스위치 이벤트 리스너 ──
    document.querySelectorAll(".toggle-segment").forEach(btn => {
        btn.addEventListener("click", () => {
            handleReportTypeChange(btn.getAttribute("data-type"));
        });
    });

    // ── 차원 선택(Axis Select) 이벤트 리스너 ──
    const axisSelect = document.getElementById("axis-select");
    if (axisSelect) {
        axisSelect.addEventListener("change", (e) => {
            appState.activeAxis = e.target.value;
            selectReport(appState.activeReportUri, appState.activeAxis);
        });
    }

    // ── 축 전환(Pivot) 버튼 이벤트 리스너 ──
    const btnPivotAxis = document.getElementById("btn-pivot-axis");
    if (btnPivotAxis) {
        btnPivotAxis.addEventListener("click", () => {
            appState.pivotSwapped = !appState.pivotSwapped;
            
            // 헤더 업데이트
            const report = appState.activeReportData;
            if (report && report.is_dimensional) {
                if (appState.pivotSwapped) {
                    updatePivotedTableHeaders(report);
                } else {
                    updateDimensionalTableHeaders(report.dimensional_columns);
                }
            }
            
            renderActiveReport();
        });
    }
}

// ==========================================================================
// 연결/별도 토글 상태 조작 및 시트 counterpart 매칭
// ==========================================================================

/**
 * 시트 라벨에서 연결/별도/공백/코드 등을 제거하여 순수 비교용 기저 이름을 추출합니다.
 * @param {string} label - 시트 라벨 (예: '[D210000] 재무상태표 - 연결')
 * @returns {string} 순수 비교용 기저 명칭
 */
function getBaseSheetName(label) {
    let name = label.split("|")[0]; // 영문 부분 제외
    name = name.replace(/\[.*?\]/, ""); // 코드 제거
    name = name.replace(/[\s\-\,\.\/\_]/g, ""); // 기호 및 공백 제거
    name = name.replace(/(연결재무제표|별도재무제표|연결|별도)/g, ""); // 관련 단어 제거
    return name.trim();
}

/**
 * 연결재무제표 / 별도재무제표 토글 전환을 핸들링합니다.
 * @param {string} newType - 'consolidated' 또는 'separate'
 */
function handleReportTypeChange(newType) {
    if (appState.reportType === newType) return;

    appState.reportType = newType;

    // UI 활성 버튼 스타일 업데이트
    document.querySelectorAll(".toggle-segment").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-type") === newType);
    });

    // 사이드바 시트 목록 재렌더링
    renderReportList();

    // 현재 열려 있는 리포트가 있으면 상대 시트(counterpart)로 자동 이동 매칭 시도
    if (appState.activeReportUri) {
        const activeSheet = appState.reports.find(r => r.role_uri === appState.activeReportUri);
        if (activeSheet) {
            const label = activeSheet.label.toLowerCase();
            const hasCons = label.includes("연결") || label.includes("consolidated");
            const hasSep = label.includes("별도") || label.includes("separate");

            // 현재 보고서가 연결 또는 별도 전용 보고서인 경우에만 상대 전환 수행
            if (hasCons || hasSep) {
                const activeBase = getBaseSheetName(activeSheet.label);

                // 새 타입에 유효한 시트들
                const candidates = appState.reports.filter(r => {
                    const rLabel = r.label.toLowerCase();
                    const rHasCons = rLabel.includes("연결") || rLabel.includes("consolidated");
                    const rHasSep = rLabel.includes("별도") || rLabel.includes("separate");
                    if (newType === 'consolidated') {
                        return !(rHasSep && !rHasCons);
                    } else {
                        return !(rHasCons && !rHasSep);
                    }
                });

                // 동일 기저 명칭을 가진 counterpart 시트 검색
                const counterpart = candidates.find(r => getBaseSheetName(r.label) === activeBase);
                if (counterpart) {
                    selectReport(counterpart.role_uri);
                    return;
                }

                // counterpart를 못 찾은 경우 새 타입의 첫 번째 주요 재무제표 시트 선택
                const firstPrimary = candidates.find(r => {
                    const matches = r.label.match(/\[(.*?)\]\s*(.*)/);
                    const code = matches ? matches[1] : "";
                    return code.startsWith("D2") || code.startsWith("D3") || code.startsWith("D4") ||
                           code.startsWith("D5") || code.startsWith("D6");
                });

                if (firstPrimary) {
                    selectReport(firstPrimary.role_uri);
                    return;
                }
            }
        }
    }

    // 만약 counterpart 매칭 전환이 일어나지 않았다면 현재 활성화된 시트 뷰 갱신
    if (appState.activeReportUri) {
        selectReport(appState.activeReportUri);
    }
}
