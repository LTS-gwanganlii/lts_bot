# LTS Bot 사용 방법

이 프로젝트는 **Python 음성 비서 백엔드**와 **크롬 익스텐션(YouTube 제어)**로 구성되어 있습니다.

---

**이용 제한**

- **상업적 사용 불가**: 이 프로젝트는 비상업적·개인 용도로만 사용할 수 있습니다.
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

아래 예시처럼 가상환경을 만들고 패키지를 설치하세요.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade pip
```

프로젝트 코드에서 사용하는 주요 패키지 예시:

```bash
pip install -r requirements.txt
# 또는
pip install websockets google-generativeai google-genai pyaudio sounddevice soundfile pywin32
```

- **STT**: Gemini Developer API (AI Studio) Live API 사용. `google-genai` 패키지 필요.
- **TTS**: MeloTTS 사용 환경에 따라 해당 패키지 및 모델이 필요할 수 있습니다.
- 블루투스 PTT 키는 앱에서 감지하지 않음. 대신 **레벨·길이 게이트**로 발화 판단(일정 이상 음성이 일정 시간 이상일 때만 전송). 선택 환경 변수: `AUDIO_GATE_THRESHOLD`, `AUDIO_GATE_MIN_DURATION_SEC`.

## 3) 실행 (빌드/테스트 서버)

### 빠른 실행 (통합 실행)

아래 명령으로 음성 비서 + 웹소켓 브릿지를 함께 실행합니다.

```bash
cd backend
python main.py
```

실행되면 백엔드가 로컬 `localhost:8765`에서 웹소켓 서버를 열고, 크롬 익스텐션이 여기에 붙어서 YouTube 제어 명령을 받습니다.

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
- **LLM 관련 오류**: `GEMINI_API_KEY` 환경변수 설정 확인

```bash
export GEMINI_API_KEY="YOUR_KEY"   # Windows PowerShell: $env:GEMINI_API_KEY="YOUR_KEY"
```
