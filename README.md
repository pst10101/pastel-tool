# 🌸 파스텔 도구

멤버 트윗을 모아 보여주고, 스페이스를 녹음해서 폰에 저장하는 Termux용 도구예요.
파이썬 파일 하나(`pastel.py`)이고, 표준 라이브러리만 써서 `pip install` 할 게 없어요.

- **트윗 피드**: [fxtwitter API](https://docs.fxembed.com/api/introduction/)(무료)로 멤버 트윗을 가져와 카드 형태로 보여줘요. 새 트윗이 오면 알림도 보내요.
- **스페이스 녹음**: `dynamic_playlist.m3u8` 링크를 ffmpeg로 녹음해서 m4a로 저장해요.
- X 계정 로그인이 필요 없어요.

## 설치 (Termux)

```
pkg install python ffmpeg
termux-setup-storage
```

선택 사항:

```
pkg install termux-api     # 새 트윗 알림 (Termux:API 앱도 설치 필요)
pip install -U yt-dlp      # x.com/i/spaces/… 주소로 바로 녹음
```

`pastel.py` 를 Termux 홈(`~`)에 두세요. 다운로드 폴더에 받았다면:

```
cp ~/storage/downloads/pastel.py ~/
```

## 사용법

```
python pastel.py add 핸들1 핸들2    # 멤버 추가 (@ 없이, x.com 주소도 가능)
python pastel.py check 핸들1        # API가 되는지 최근 트윗 3개로 확인
termux-wake-lock                    # 폰이 잠들어도 계속 돌게 (권장)
python pastel.py                    # 서버 시작
```

시작한 뒤 폰 브라우저에서 **http://127.0.0.1:8787** 을 열어요.

| 명령 | 설명 |
|---|---|
| `python pastel.py` | 피드 서버 시작 (`serve` 와 같아요) |
| `python pastel.py add 핸들…` | 멤버 추가 |
| `python pastel.py rm 핸들…` | 멤버 삭제 |
| `python pastel.py members` | 멤버 목록 |
| `python pastel.py check 핸들` | API 동작 확인 |
| `python pastel.py rec 링크 -n 이름` | 화면 없이 바로 녹음 (Ctrl+C 로 종료·저장) |

## 화면

- **트윗 탭**: 멤버별 필터, 사진·영상·인용·번역 표시, 원본 열기. 트윗에 스페이스 링크가 있으면 `녹음 시작` 버튼이 떠요.
- **녹음 탭**: 링크를 붙여넣고 시작하면 용량과 시간이 실시간으로 올라가요. `중지하고 저장` 을 누르면 파일을 마무리해요.

## 녹음

- 저장 위치: `termux-setup-storage` 를 했다면 `~/storage/shared/Pastel`(폰의 `Pastel` 폴더), 아니면 `~/pastel_recordings`.
- 파일 이름: `날짜_시각_이름.m4a`
- **m3u8 링크를 직접 넣는 게 가장 확실해요.** 스페이스 주소는 yt-dlp를 거치기 때문에 X 쪽이 바뀌면 안 될 수 있어요.
- 중지하면 저장 완료까지 몇 초(보통 5~6초) 걸려요.
- 앱이 강제로 꺼져도 2초마다 기록하기 때문에 그때까지 녹음된 분량은 재생돼요 (마지막 2초 안팎만 손실).

## 설정 (`~/.pastel/config.json`)

직접 고쳐도 되고, 고친 뒤에는 서버를 다시 시작하세요.

| 항목 | 기본값 | 설명 |
|---|---|---|
| `members` | `[]` | 트위터 핸들 목록 (`add` / `rm` 이 수정해요) |
| `interval` | `60` | 전체 멤버를 한 바퀴 도는 시간(초). 멤버가 많으면 늘리세요 |
| `count` | `10` | 한 번에 가져오는 트윗 수 (1~100) |
| `include_replies` | `false` | 답글 포함 |
| `include_reposts` | `false` | 리포스트 포함 (켜면 매번 전체를 받아 데이터를 더 써요) |
| `translate_lang` | `""` | `"ko"` 처럼 두면 번역문도 같이 표시 |
| `notify` | `true` | 새 트윗 알림 (termux-api 필요) |
| `auto_record_spaces` | `false` | 새 트윗에서 스페이스 링크를 찾으면 자동 녹음 (yt-dlp 필요) |
| `port` | `8787` | 화면 포트 |
| `save_dir` | `""` | 녹음 저장 폴더 (비우면 자동) |
| `api_base` | `https://api.fxtwitter.com` | API 주소 |

## 문제 해결

| 증상 | 해결 |
|---|---|
| `ffmpeg 가 없어요` | `pkg install ffmpeg` |
| 스페이스 주소가 안 돼요 | `pip install -U yt-dlp` 후 다시 시도, 안 되면 m3u8 링크를 직접 넣기 |
| `계정을 못 찾았거나 트윗이 없어요` | 핸들 오타, 비공개 계정, 트윗이 없는 계정인지 확인 |
| `요청이 너무 많아요(429)` | `interval` 을 늘리세요 |
| 알림이 안 와요 | `pkg install termux-api`, Termux:API 앱 설치, 알림 권한 허용 |
| 얼마 뒤 멈춰요 | `termux-wake-lock` 실행, 폰의 배터리 최적화에서 Termux 제외 |
| 포트가 이미 사용 중 | `config.json` 의 `port` 변경 |

## 파일과 개인정보

- 설정·피드 캐시는 `~/.pastel/` 에만 저장돼요.
- 계정 정보, 토큰, 쿠키를 쓰지 않아요.
- 서버는 `127.0.0.1`(폰 안)에서만 열려요. 다른 기기나 다른 사이트에서는 접근할 수 없게 막혀 있어요.
- 밖으로 나가는 요청은 fxtwitter에 보내는 "이 핸들의 최근 트윗" 요청뿐이에요.

## 알아둘 점

- fxtwitter는 비공식 서드파티 서비스라서 X 쪽 변화로 갑자기 안 될 수 있어요. 공개 API의 요청 제한은 IP당 분당 1000회라 멤버 십수 명이면 넉넉해요.
- 녹음은 개인 보관용으로 쓰세요. 다른 사람의 스페이스를 재배포하는 것은 별개의 문제예요.
