# -*- coding: utf-8 -*-
"""
XBRL Viewer 실행 스크립트 (run.py)

이 스크립트는 XBRL 뷰어 어플리케이션을 한 번의 명령으로 실행할 수 있도록
가상환경 생성, 의존성 설치, 서버 시작, 브라우저 열기를 자동화합니다.

실행 방법:
    python run.py

작동 순서:
    1. 가상환경(.venv) 존재 여부 확인 → 없으면 자동 생성
    2. 가상환경 내부에서 재실행 (sys.prefix 체크)
    3. 필수 Python 패키지 설치 확인
    4. FastAPI 백엔드 서버 구동 (uvicorn)
    5. 기본 웹 브라우저 자동 열기
"""

import os
import sys
import subprocess
import time
import webbrowser


def check_and_activate_venv():
    """가상환경(.venv) 내부에서 실행 중인지 확인하고, 아니면 가상환경을 생성/활성화합니다.

    가상환경이 없으면 새로 생성한 뒤, 가상환경의 Python 인터프리터로
    이 스크립트를 다시 실행하고 현재 프로세스를 종료합니다.
    """
    # 현재 가상환경 내부인지 확인
    is_venv = (sys.prefix != sys.base_prefix) or hasattr(sys, 'real_prefix')
    if is_venv:
        return  # 이미 가상환경 내부이므로 그대로 진행

    # 가상환경 경로 결정 (OS별)
    venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
    if sys.platform == "win32":
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        venv_python = os.path.join(venv_dir, "bin", "python")

    # 가상환경이 없으면 생성
    if not os.path.exists(venv_dir) or not os.path.exists(venv_python):
        print("가상환경(.venv)을 생성합니다...")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", ".venv"])
            print("가상환경 생성 완료.")
        except subprocess.CalledProcessError as e:
            print(f"가상환경 생성 실패: {e}")
            sys.exit(1)

    # 가상환경의 Python으로 이 스크립트를 재실행
    print("가상환경(.venv) 내에서 스크립트를 재실행합니다...")
    try:
        cmd = [venv_python] + sys.argv
        sys.exit(subprocess.call(cmd))
    except Exception as e:
        print(f"가상환경 실행 오류: {e}")
        sys.exit(1)


def install_dependencies():
    """필수 Python 패키지가 설치되어 있는지 확인하고, 없으면 자동 설치합니다.

    임포트 이름과 pip 패키지 이름이 다른 경우를 처리합니다.
    (예: multipart → python-multipart)
    """
    # {임포트 이름: pip 패키지 이름} 매핑
    packages = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "pandas": "pandas",
        "openpyxl": "openpyxl",
        "multipart": "python-multipart",
    }
    missing_packages = []

    for import_name, pip_name in packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_packages.append(pip_name)

    if missing_packages:
        print(f"누락된 패키지 설치 중: {missing_packages}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *missing_packages]
            )
            print("패키지 설치 완료.")
        except subprocess.CalledProcessError as e:
            print(
                f"패키지 설치 실패. 수동으로 실행해 주세요: "
                f"pip install {' '.join(missing_packages)}"
            )
            sys.exit(1)


def main():
    """메인 실행 함수 — 가상환경 확인, 의존성 설치, 서버 시작을 순차 실행합니다."""

    # 0단계: 가상환경 내부에서 실행 중인지 확인
    check_and_activate_venv()

    print("=" * 50)
    print("      XBRL Standard Interactive Viewer")
    print("=" * 50)

    # 1단계: 필수 패키지 설치 확인
    install_dependencies()

    # 2단계: 기본 데이터 디렉토리 확인
    project_root = os.path.dirname(os.path.abspath(__file__))
    sample_dir = os.path.join(project_root, "xbrl sample")

    print(f"\n기본 데이터 디렉토리 확인:")
    print(f"  {sample_dir}")

    if os.path.exists(sample_dir):
        zip_files = [f for f in os.listdir(sample_dir) if f.endswith('.zip')]
        if zip_files:
            print(f"  [OK] {len(zip_files)}개의 XBRL 패키지를 발견했습니다:")
            for zf in zip_files:
                print(f"       - {zf}")
        else:
            print("  [경고] 샘플 디렉토리에 ZIP 파일이 없습니다.")
    else:
        print("  [경고] 기본 데이터 디렉토리를 찾을 수 없습니다.")
        print("  웹 UI에서 XBRL 패키지(.zip)를 직접 업로드할 수 있습니다.")

    # 3단계: 서버 시작 및 브라우저 열기
    import uvicorn
    import threading

    port = 8000
    url = f"http://127.0.0.1:{port}"

    print(f"\n백엔드 서버를 시작합니다...")
    print(f"앱 주소: {url}")

    def open_browser():
        """서버가 준비되면 기본 브라우저를 엽니다."""
        time.sleep(1.5)
        print(f"\n기본 브라우저에서 {url} 을 엽니다...")
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        uvicorn.run(
            "backend.app:app",
            host="127.0.0.1",
            port=port,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n서버를 종료합니다. 안녕히 가세요!")


if __name__ == "__main__":
    main()
