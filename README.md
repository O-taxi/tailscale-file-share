# 自宅サーバー + Tailscale: プライベートファイル共有

依存パッケージなしの Python ファイル共有アプリです。アプリ自体は `127.0.0.1:8080` にのみ待ち受けます。外出中のスマホへは、**Tailscale Serve** が Tailnet 内限定の HTTPS として公開します。インターネット全体に公開する Funnel は使いません。

## 初回セットアップ

自宅サーバー の Linux で次を実行します。Tailscale の認証 URL が表示されたら、スマホで使うのと同じ Tailnet のアカウントで認証してください。

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

スマホ側にも Tailscale アプリを入れ、同じ Tailnet へサインインして接続状態にします。  
[tailscale コンソール](https://console.tailscale.com/)で登録されたデバイスの一覧が確認できます。

## 接続シーケンス

アプリは自宅サーバーの外部ネットワークには直接待ち受けません。名前解決・端末認証後、スマホとサーバーの Tailscale は原則として直接通信し、サーバー上の Tailscale Serve が `127.0.0.1:8080` の Python アプリへリバースプロキシします。直通できないネットワーク環境では、暗号化済みパケットだけが DERP リレーを通ります。

```mermaid
sequenceDiagram
    autonumber
    participant Phone as スマホのブラウザ
    participant PhoneTS as スマホのTailscale
    participant DERP as DERPリレー<br/>（直通不可時のみ）
    participant Serve as 自宅サーバーのTailscale Serve<br/>HTTPS :443
    participant App as Pythonアプリ<br/>127.0.0.1:8080

    Phone->>PhoneTS: https://<machine>.<tailnet>.ts.net/ を開く
    Note over PhoneTS,Serve: ACL で許可された端末だけが接続可能
    alt 直接接続できる場合
        PhoneTS->>Serve: WireGuardで暗号化されたHTTPS通信（P2P）
    else 直接接続できない場合
        PhoneTS->>DERP: 暗号化済みパケットを中継
        DERP->>Serve: 暗号化済みパケットを中継
    end
    Serve->>App: GET / または API リクエスト（localhostへ転送）
    App-->>Serve: HTML または JSON / ファイル本体
    Serve-->>PhoneTS: HTTPSレスポンス
    PhoneTS-->>Phone: ファイル一覧・プレビュー・ダウンロード画面を表示
```

## 起動と公開

```bash
./scripts/start.sh
./scripts/publish-tailscale.sh
```

2 個目のコマンドが表示する `https://<マシン名>.<tailnet名>.ts.net/` をスマホで開いてください。ファイル一覧が表示されれば、Tailnet 経由の接続を確認できます。

停止するには以下を実行します。

```bash
./scripts/stop.sh
tailscale serve reset
```

## ファイルの使い方

- `data/downloadable/` に置いたファイルは「ダウンロード」一覧に表示されます。10 件ごとのページング、画像・PDF・テキストのプレビュー、ダウンロードに対応しています。
- 画面からアップロードしたファイルは `data/uploaded/` に保存されます。アップロード済み一覧からダウンロードまたは削除できます。同名ファイルは上書きせず、連番を付けて保存します。
- 1 回のアップロードは合計 100 MiB までです。テキストプレビューは 256 KiB までです。


## 自宅サーバーについて

この構成は 自宅サーバー を起動している間に動きます。サーバー が終了した後はアクセスできません。常時公開が必要なら、次の段階としてサーバーの自動起動・サービス常駐化 を設定してください。

`tailscale serve` の設定は Tailscale 側に残るため、アプリを止めている間は HTTPS 側が 502 になります。公開を止める場合は必ず `tailscale serve reset` を実行してください。