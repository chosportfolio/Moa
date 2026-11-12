# 개발 안내

이 문서는 Windows의 Pleos Connect 에뮬레이터와 PC 로컬 서버를 함께 사용하는 개발 구성을 설명합니다. 명령은 `cabin-conversation` 폴더에서 실행합니다.

## 1. Python 서버와 음성 모델

Python 3.12와 인터넷 연결이 필요합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe download_models.py --model-dir models
.\start.ps1 -BrowserOnly
```

서버는 `127.0.0.1:8765`에만 바인딩합니다. `start.ps1`은 기존 서버가 있으면 재사용하며 다른 프로그램이 포트를 사용하면 중지합니다. 모델 다운로드에는 Whisper small, 화자 임베딩, 음성 구간 분리 가중치가 포함됩니다. 모델을 준비하지 않은 환경에서는 로컬 음성 처리 기능을 사용할 수 없습니다.

브라우저는 화면과 로컬 음성·추천 흐름을 확인하는 용도입니다. 차량 SDK, Gleo 명령, 순정 내비는 Android 앱에서 확인해야 합니다.

## 2. Pleos Android 환경

필요한 구성은 다음과 같습니다.

- Android Studio의 JDK 또는 호환되는 JDK
- Android SDK API 34, build-tools, platform-tools, emulator
- 공식 Pleos Connect 시스템 이미지와 이름이 `Pleos_Connect`인 AVD
- `android/deps/`의 Pleos SDK와 관련 JVM 의존성

```powershell
.\.venv\Scripts\python.exe setup_android.py
```

`setup_android.py`는 공식 저장소에서 Android 도구와 Pleos 시스템 이미지를 내려받습니다. **JDK·가상기기·앱 SDK 의존성을 생성하지는 않습니다.** AVD는 [Pleos 개발 문서](https://document.pleos.ai/)에 따라 별도로 준비해야 합니다.

앱 의존성은 공식 Pleos·Google·Maven Central 저장소에서 복원합니다. Python 표준 라이브러리만 필요하며, 고정된 주소·파일명·체크섬은 `scripts/android-dependencies.json`에서 관리합니다.

```powershell
.\.venv\Scripts\python.exe scripts/restore_android_deps.py

# 네트워크 요청 없이 준비된 의존성의 체크섬만 확인
.\.venv\Scripts\python.exe scripts/restore_android_deps.py --check
```

20개 Maven 원본에서 빌드에 필요한 31개 파일을 복원합니다. 별도 위치가 필요하면 `--output-dir`을 사용할 수 있습니다. 현재 빌드는 `android/deps/*.jar`를 직접 컴파일하므로 기본 경로 밖에 복원한 결과를 자동으로 사용하지는 않습니다. SDK 의존성이 없으면 빌드가 중단되며, Gradle 의존성 해결을 사용하는 구성은 아닙니다. 내려받은 각 SDK와 라이브러리의 이용 조건은 원배포처를 따릅니다.

```powershell
.\android\build.ps1
.\start.ps1
```

빌드 결과는 `android/build/cabin-conversation.apk`입니다. `start.ps1`은 **이미 만들어진 APK를 설치**하며 소스를 빌드하지 않습니다. SDK 기본 경로는 `tools/android-sdk`, JDK 기본 경로는 Android Studio의 `jbr`입니다. 다른 설치 위치는 빌드 인자로 지정할 수 있습니다.

```powershell
.\android\build.ps1 -SdkRoot 'D:\Android\Sdk' -JdkRoot 'D:\Java\jdk'
```

다만 `start.ps1`은 프로젝트 안의 SDK 경로를 사용하므로 위 인자는 빌드 도구 위치만 바꿉니다. APK 설치 시 기존 앱의 패키지·서명을 유지해야 저장 데이터를 보존할 수 있습니다.

### 내비 상태 추적과 알림

v1.6.5의 `NavigationTrackingService`는 사용자가 시작한 순정 내비 안내의 콜백을 유지하기 위한 foreground service입니다. 별도 내비 앱이 전면에 있을 때 모아가 Android의 cached process freezer에 의해 멈춰 안내 상태를 놓치는 문제를 보완합니다. **모아 · 길안내 연결 중** 알림을 사용하며, 이 서비스가 마이크 녹음이나 GPS 측정을 새로 시작하지는 않습니다.

도착·취소·시작 실패·요청 만료·최근 앱 목록에서 제거 시 추적을 정리하는 구조입니다. `START_NOT_STICKY`로 선언해 프로세스 종료만으로 예전 세션을 자동 재시작하지 않습니다. 직접 도착 확인은 모아의 추적을 끝내며 순정 내비 경로 자체를 강제로 취소하지 않습니다. 실제 기기에서 확인한 시작·종료 조건은 [검증 기록](VERIFICATION.md)과 구분해 관리합니다.

Android 14 이상에서는 manifest에 `foregroundServiceType="specialUse"`, `FOREGROUND_SERVICE_SPECIAL_USE` 권한과 `android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE`의 용도 설명을 선언합니다. 현재 선언은 사용자 요청에 따른 내비 콜백 감시용입니다. 배포 시에는 이 용도와 동작을 정확히 설명해야 하며, Google Play 배포에는 foreground service 유형 신고와 specialUse 용도 검토가 추가됩니다. 에뮬레이터 설치 성공이 마켓 승인 결과를 뜻하지는 않습니다. [Android 공식 서비스 유형 안내](https://developer.android.com/develop/background-work/services/fgs/service-types#special-use)

## 3. 공개 장소 색인

전국 장소 색인은 선택 기능입니다. 색인이 없을 때는 공개 원격 검색을 사용하지만 속도·가용성·검색 범위가 달라질 수 있습니다.

1. [Geofabrik 대한민국 자료](https://download.geofabrik.de/asia/south-korea.html)에서 `.osm.pbf` 파일을 내려받습니다.
2. 파일을 `data/maps/south-korea-latest.osm.pbf`에 둡니다.
3. 선택 의존성을 설치하고 색인을 생성합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-poi.txt
.\.venv\Scripts\python.exe tools/build_poi_index.py --input data/maps/south-korea-latest.osm.pbf --output data/maps/pois.sqlite
```

빌더는 파일을 자동 다운로드하지 않습니다. 새 색인이 완성된 뒤 기존 색인을 교체하며 공개 장소와 해안선 자료만 기록합니다. `CABIN_POI_INDEX`로 색인 파일 경로를 바꿀 수 있습니다.

장소의 node·way를 포함하지만 relation과 지도에 등록되지 않은 가게는 포함하지 않습니다. 바다 후보는 해변 분류와 실제 해안선 1.5km 이내 조건을 함께 확인합니다. 이 조건이 차량 진입이나 바다 전망을 보증하지는 않습니다.

명시한 목적 지역을 색인에서 검색하는 경우 해변은 지역 기준점 20km, 그 외는 4.5km 주변을 확인합니다. 이는 행정구역 전체 검색을 의미하지 않습니다. 목적 지역이 없고 이동 시간이 정해진 경우에는 출발지 주변의 넓은 범위에서 여러 방향의 후보를 수집한 뒤 도로 시간을 확인합니다.

## 4. 개발용 위치와 네트워크 설정

기본 실행은 Pleos FusedLocation의 유효한 차량 위치를 사용합니다. 오래된 위치나 권한이 없는 위치를 현재 위치로 간주하지 않습니다.

```powershell
# 에뮬레이터 개발 중에만 Windows 위치를 임시 입력으로 사용
.\start.ps1 -UsePcLocation

# 에뮬레이터를 새로 시작할 때 호스트 DNS 사용
.\start.ps1 -UseHostDns
```

PC 위치 옵션은 기본적으로 꺼져 있고 제품 기능이 아닙니다. 차량 위치가 유효하면 차량 위치가 우선합니다. 이미 다른 위치 모드로 실행 중인 서버를 자동 종료하지는 않습니다.

기본 시작 스크립트는 새 에뮬레이터에 DNS `8.8.8.8`을 지정합니다. 이 설정은 시험 환경의 이름 해석 오류 대응이며, 순정 내비의 배경 지도나 위치 고정 문제를 해결하는 설정은 아닙니다.

원격 서비스는 다음 제한을 적용합니다.

- Nominatim: 프로세스 전체 순차 처리, 최소 1.1초 간격, 캐시. `CABIN_NOMINATIM_SEARCH_URL`로 서버 주소 교체 가능
- OSRM: 추천당 기본 최대 160개 후보를 40개씩 확인, 최소 1.1초 간격, 요청당 12초 제한, 메모리 캐시. 잘못된 도로 연결점으로 묶음이 실패하면 추가 조회 최대 4회
- 공개 서비스의 오류·제한·데이터 부족은 추천 개수 감소나 검색 실패로 이어질 수 있음

[Nominatim 정책](https://operations.osmfoundation.org/policies/nominatim/)의 초당 1회 제한은 앱 전체 사용자 요청의 합계에 적용됩니다. 현재 프로세스 단위 제한만으로 여러 PC의 운영 트래픽을 제어할 수는 없습니다. 자동완성이나 지역의 전체 장소 수집에 공개 API를 사용하지 않으며, 전국 자료는 PBF 추출본을 사용합니다. [FOSSGIS 경로 서비스](https://routing.openstreetmap.de/about.html)도 초당 1회 이하와 과도한 사용 금지를 요구하고 요청을 서버 로그에 기록합니다.

## 5. 자동 검사

Python 회귀 검사는 임시 상태와 모의 응답을 사용하는 검사를 포함합니다. 실제 서비스 연결이나 실제 차량 동작을 모두 증명하는 검사는 아닙니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py"
```

실제 음성 모델 추론 검사는 기본적으로 생략됩니다. 별도 시험 음원과 모델을 준비한 뒤 `CABIN_SPEECH_INTEGRATION=1`로 선택 실행할 수 있습니다. `test_poi_index`의 색인 빌더 검사는 `osmium` 설치가 필요합니다.

화면 상태·이벤트 회귀 검사는 Node.js의 기본 모듈로 실행합니다. 실제 브라우저 렌더링 검사를 대신하지는 않습니다.

```powershell
Get-ChildItem .\tests -Filter 'test_*.cjs' | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "실패: $($_.Name)" }
}
```

`android/tests/`에는 내비 목적지 일치·연결 복구·음성 전달·위치 수명 등의 Java 검사가 있습니다. `android/build.ps1`은 APK를 빌드하고 서명을 확인하지만 Java 단위 검사를 자동 실행하지 않습니다. UI와 SDK 동작은 실제 Pleos 환경에서 별도로 확인해야 합니다.

## 6. 상태와 데이터 보관

- `data/meeting.json`: 현재 대화·추천·여정·저장 장소·개인 평가·샘플 이전 대화의 내부 백업
- `data/maps/`: 공개 지도 원본과 재생성 가능한 장소 색인
- `models/`: 내려받은 음성 모델
- `android/build/`: APK와 빌드 결과
- `server.log`, `server-error.log`: 개발 로그

차량 위치와 현재 위치 검색 캐시는 메모리에서 관리합니다. 지역명 검색 캐시는 로컬 파일을 사용할 수 있습니다. 대화 상태에 포함된 장소·출발지 설명은 별도 데이터이므로 위치 캐시와 동일하게 취급하면 안 됩니다.

녹음·대화·좌표·평가·진단 로그·서명키·모델·SDK 바이너리는 공개 저장소나 문제 보고에 첨부하지 않습니다. 재현에는 작성한 샘플과 공개 장소 기준점을 사용하고, 실제 대화가 포함된 기존 데이터 파일을 덮어쓰지 않습니다.
