# myATS

Windows x64와 Linux x64용 myATS 대시보드입니다. Rust 설치기와 업데이트 실행기가 기존 Tk 화면, Python 조회 모듈, Rust ATS 계산 코어를 실행합니다.

[Releases](https://github.com/mikunoi/myATS/releases)에서 운영체제에 맞는 파일을 받습니다. Windows에서 `myATS-Setup-*-windows-x64.exe`를 실행합니다. Linux에서는 다운로드한 파일에 실행 권한을 준 뒤 실행합니다.

```sh
chmod +x myATS-Setup-*-linux-x64.run
./myATS-Setup-*-linux-x64.run
```

설치 후 프로그램 메뉴에서 **myATS**를 엽니다. **설정 → .env 편집**에서 자신의 증권사 인증을 입력합니다. 설치 데이터와 별개인 사용자 설정 폴더에 인증 파일을 저장합니다. 새 버전이 있으면 실행 시 알림이 표시되며 **설정 → GitHub 업데이트 확인**에서도 확인할 수 있습니다.

프로그램 배포 작업은 버전 태그 또는 GitHub Actions의 수동 게시 옵션으로 실행합니다. 개발자는 `launcher/Cargo.toml`과 대시보드의 버전을 함께 변경합니다. 이 배포의 대시보드는 기존 PC의 거래 DB 없이 시작하며 주문 서비스 설치는 별개입니다.
