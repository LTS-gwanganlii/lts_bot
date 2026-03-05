# LTS Bot 사용 방법

이 프로젝트는 **Python 음성 비서 백엔드**와 **크롬 익스텐션(YouTube 제어)**로 구성되어 있습니다.

---

**이용 제한**

- **상업적 사용 불가**: 이 프로젝트는 별도 계약이 없는 한 비상업적·개인 용도로만 사용할 수 있습니다.
- **라이선스 보유 기업만 사용 가능**: 상업·영리 목적으로 사용하려면 해당 소프트웨어 및 API(예: Gemini, MeloTTS 등)의 이용약관 및 라이선스를 충족하는 기업·단체만 사용할 수 있습니다. 사용 전 각 의존 소프트웨어의 라이선스와 이용 조건을 확인하세요.

---

## 1) 크롬 익스텐션 설치 방법

1. 크롬 주소창에 `chrome://extensions` 입력
2. 우측 상단 **개발자 모드** 활성화
3. **압축해제된 확장 프로그램을 로드합니다** 클릭
4. 이 저장소의 `extension/` 폴더 선택
5. 설치 후 YouTube 탭을 하나 열어둡니다.

> 익스텐션은 `ws://localhost:8765` 웹소켓 서버에 연결해 명령을 받습니다.

## 2) Python 환경 준비

- **Python 3.9 이상** 필요 (3.10, 3.11, 3.12 권장). 3.1이 아님.

가상환경 만들기:

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell 또는 CMD):
.venv\Scripts\activate
python -m pip install --upgrade pip
```

기본 패키지 설치:

```bash
pip install -r requirements.txt
```

- **STT**: Gemini Developer API (AI Studio) Live API 사용. `google-genai` 패키지 필요.
- **TTS (MeloTTS)**: 플랫폼별로 아래 순서를 따르세요.
  - **macOS**: MeloTTS 의존성(fugashi)이 MeCab을 사용하므로, MeCab을 먼저 설치한 뒤 MeloTTS를 GitHub에서 설치합니다.
    ```bash
    brew install mecab mecab-ipadic
    pip install git+https://github.com/myshell-ai/MeloTTS.git
    python -m unidic download
    ```
  - **Windows**: PyPI의 `melotts`는 빌드 오류가 자주 나므로, GitHub에서 설치를 시도합니다. fugashi/MeCab 오류가 나면 [MeloTTS-Windows](https://github.com/EliseWindbloom/MeloTTS-Windows) 또는 conda 환경을 참고하세요.
    ```powershell
    pip install git+https://github.com/myshell-ai/MeloTTS.git
    python -m unidic download
    ```
  최신 MeloTTS는 `model-path` 파라미터를 지원하지 않으며, 기본적으로 Hugging Face에서 언어별 모델을 자동 다운로드합니다. 로컬 모델을 쓰려면 `config.json`과 `checkpoint.pth`를 한 디렉터리(예: `backend/models/melo`)에 두고 `TTSHandler(model_dir="backend/models/melo")`로 지정하면 됩니다.
  - **tokenizers / llvmlite 빌드 실패 시** (Rust 오류, `dry_run` 오류): pip와 setuptools를 올린 뒤, 미리 빌드된 wheel로 의존성을 먼저 설치하고 MeloTTS를 다시 시도하세요.
    ```bash
    pip install --upgrade pip setuptools wheel
    pip install tokenizers llvmlite
    pip install git+https://github.com/myshell-ai/MeloTTS.git
    ```
    그래도 실패하면 **Python 3.10 또는 3.11**로 가상환경을 새로 만들어 시도해 보세요. (3.12에서 일부 의존성 wheel이 없을 수 있음.)
- 블루투스 PTT 키는 앱에서 감지하지 않음. 대신 **레벨·길이 게이트**로 발화 판단(일정 이상 음성이 일정 시간 이상일 때만 전송). 선택 환경 변수: `AUDIO_GATE_THRESHOLD`, `AUDIO_GATE_MIN_DURATION_SEC`.

## 3) 실행 (빌드/테스트 서버)

### 빠른 실행 (통합 실행)

음성 비서 + 웹소켓 브릿지를 실행합니다. (macOS/Linux와 Windows 모두 동일)

```bash
cd backend
python main.py
```

실행되면 백엔드가 로컬 `localhost:8765`에서 웹소켓 서버를 열고, 크롬 익스텐션이 여기에 붙어서 YouTube 제어 명령을 받습니다. **Windows**에서 창 제어 기능을 쓰려면 `pip install pywin32`가 필요합니다.

### 최소 동작 테스트 (문법/모듈 확인)

```bash
python -m py_compile backend/*.py
```


## 4) 동작 흐름 요약

1. 마이크 음성 입력 → 레벨·길이 게이트 통과 시에만 버퍼 적재 → 무음 종료 시 Gemini Live API로 STT
2. 전사 텍스트를 명령으로 분석 (호출어 없음)
3. Python 백엔드가 웹소켓으로 명령 브로드캐스트
4. 크롬 익스텐션이 YouTube 탭에 play/pause/seek/search 명령 전달

## 5) 자주 발생하는 문제

- **익스텐션이 반응이 없음**: `backend/main.py`가 실행 중인지, 포트 `8765`가 열려 있는지 확인
- **음성 기능 오류**: 오디오 장치 권한, PyAudio 설치 상태 확인. STT는 Gemini Live API 사용으로 `GEMINI_API_KEY` 필요.
- **PyAudio 빌드 실패**
  - **macOS** (`portaudio.h` not found): PortAudio를 먼저 설치한 뒤 `pip install -r requirements.txt`를 다시 실행하세요.
    ```bash
    brew install portaudio
    ```
    Homebrew 권한 오류 시: `sudo chown -R $(whoami) /usr/local/Homebrew /usr/local/Cellar /usr/local/bin /usr/local/lib /usr/local/include /usr/local/opt /usr/local/share` 후 다시 `brew install portaudio`
  - **Windows**: 보통 `pip install pyaudio`로 미리 빌드된 wheel이 설치됩니다. 실패하면 [PyAudio Windows 빌드](https://people.csail.mit.edu/hubert/pyaudio/#downloads)에서 Python 버전에 맞는 wheel을 받거나, Visual Studio Build Tools와 PortAudio를 설치한 뒤 빌드해야 합니다.
- **LLM 관련 오류**: `GEMINI_API_KEY` 환경변수 설정 확인. **프로젝트 루트에 `.env` 파일**을 두면 `python-dotenv`가 자동으로 로드합니다.
  ```bash
  # .env (프로젝트 루트에 생성)
  GEMINI_API_KEY=YOUR_KEY
  # 선택: AUDIO_GATE_THRESHOLD=500, AUDIO_GATE_MIN_DURATION_SEC=0.4
  ```
  또는 셸에서 직접 설정:
  ```bash
  # macOS/Linux
  export GEMINI_API_KEY="YOUR_KEY"
  # Windows PowerShell
  $env:GEMINI_API_KEY="YOUR_KEY"
  ```
