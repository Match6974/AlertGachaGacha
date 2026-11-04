# Cloudflare Cron → GitHub Actions

Workers の `scheduled()` から15分ごとに GitHub REST API の `workflow_dispatch` を呼び出します。Python の監視・Discord 通知・状態保存は既存の GitHub Actions が担当します。

| 項目 | 設定 |
| --- | --- |
| Repository | `Match6974/AlertGachaGacha` |
| Workflow | `.github/workflows/monitor.yml` |
| Branch | `main` |
| Inputs | `{"dry_run":false}` |
| Cron | `*/15 * * * *`（UTC の毎時00・15・30・45分。日本時間でも分は同じ） |

**デプロイして Secret を登録すると次の Cron から本番監視が起動します。** 対象店舗があれば通知・履歴保存が行われます。このリポジトリのテストでは実 API は呼びません。

## GitHub トークンの作成

GitHub の個人設定 → **Developer settings → Personal access tokens → Fine-grained tokens → Generate new token** で次を設定します。

1. Resource owner: `Match6974`
2. Repository access: **Only select repositories** → `AlertGachaGacha` のみ
3. Repository permissions: **Actions: Read and write**（Metadata: Read は自動付与）
4. 有効期限を設定し、失効前に更新する

起動要求に Contents の書き込み権限は不要です。Actions 自身の履歴保存には既存 Workflow の `GITHUB_TOKEN` が使われ、Worker 用 PAT とは別です。

PAT は Cloudflare の Secret **`GITHUB_TOKEN`** に登録します。ソース、`wrangler.jsonc` の `vars`、チャットやログには記載しないでください。

## デプロイと Secret 登録

Node.js 22 以上、npm、Cloudflare Workers を使えるアカウントが必要です。この変更を `main` にマージした後に実施します。マージ・デプロイは自動では行いません。

```sh
cd cloudflare
npm install
npm test
npx wrangler login
npm run deploy
npm run secret:put
```

最後のコマンドの入力欄へ PAT を入力します。コマンド引数には書かないでください。Worker 名は `alertgachagacha-scheduler` です。ダッシュボードの対象 Worker → **Settings → Variables and Secrets** でも、Type **Secret**、Name **GITHUB_TOKEN** として登録できます。

デプロイ直後に Cron が走っても、Secret 未設定ならエラーになり GitHub は呼びません。Secret 登録後の次の Cron から有効になります。準備中の起動を避けたい場合は、最初は `wrangler.jsonc` の `triggers.crons` を `[]` にしてデプロイし、Secret 登録後に `["*/15 * * * *"]` へ戻して再デプロイします。

Wrangler はデプロイ用の開発依存です。Worker 本体とテストに外部ライブラリは不要です。`npm install` で生成した lockfile は確認して保存すると、以後 `npm ci` を使用できます。

## Root directory（Cloudflare Git 連携）

CLI の作業ディレクトリは **`cloudflare/`** です。Cloudflare の Workers Builds で Git 連携する場合も、Pages ではなく Workers を作成し、次を設定します。

| 項目 | 値 |
| --- | --- |
| Repository | `Match6974/AlertGachaGacha` |
| Production branch | `main` |
| Root directory | `cloudflare` |
| Build command | `npm test` |
| Deploy command | `npm run deploy` |

Git 連携では依存インストール後に上記コマンドを実行します。Secret はビルド用環境変数だけでなく、対象 Worker の**実行環境**へ登録してください。CLI と Git 連携のどちらをデプロイ元にするか決めて運用します。

## Cron Triggers の確認

対象 Worker の **Settings → Triggers / Trigger Events → Cron Triggers** で `*/15 * * * *` が1件あることを確認します（UI の名称は変更される場合があります）。追加・変更の反映には最大15分程度かかる場合があります。

`workers_dev:false`、`preview_urls:false` とし、公開ルートは設定していません。同名の既存 Worker を使う場合は不要なカスタムドメインやルートも確認してください。仮に HTTP が届いても `fetch()` は404を返すだけです。

## 手動テスト

### 安全なテスト（実際の監視を起動しない）

```sh
cd cloudflare
npm test
```

すべての API を模擬します。204受付、Secret未設定、APIエラー、通信失敗、トークンのログ伏せ字、scheduled の失敗伝播、HTTPから起動できないことを確認します。本物の PAT は不要です。

Wrangler のローカル Scheduled Events を確認する場合は、**本物の GITHUB_TOKEN をローカルに設定しない状態**で実施します。プロセスの環境変数、`.dev.vars*`、`.env*` にも存在しないことを確認してください。

```sh
npm run dev
```

別ターミナルからローカルのテストイベントを送ります。

```sh
curl 'http://localhost:8787/__scheduled?cron=*/15%20*%20*%20*%20*'
```

この URL は Wrangler の `--test-scheduled` が提供する開発用機能で、本番の公開起動経路ではありません。Secret 未設定エラーになるのがこのテストの期待結果です。**本物の PAT を設定して実行すると、本番監視・通知・履歴更新が起こり得ます。**

### 本番での確認（運用開始時にユーザーが実施）

Secret と Cron の登録後、次の Cron を待ちます。Worker の204受付ログと Actions の実行結果の両方を確認してください。イベントは `workflow_dispatch`（画面では Manually run）となり、`schedule` の履歴には入りません。

## 実行ログと成功の意味

```sh
cd cloudflare
npm run tail
```

ダッシュボードの Logs / Observability でも確認できます。

- `cron_trigger`: Cron 式と予定実行時刻（Unix epoch ミリ秒）
- `github_dispatch_response`: HTTPステータス
- `github_dispatch_accepted`: **204は要求の受付であり、Workflow成功を意味しません**
- `github_dispatch_rejected`: ステータス、request ID、Retry-After、最大8KiBの応答詳細（トークンは伏せ字）
- `github_dispatch_transport_error`: 通信エラーや30秒タイムアウト。受付の成否は不明
- `dispatch_configuration_error`: Secret未設定・不正

API エラー時はハンドラーを失敗させます。監視の成否は [GitHub Actions](https://github.com/Match6974/AlertGachaGacha/actions/workflows/monitor.yml) で確認します。401ならトークン期限・値、403なら権限や制限、404ならリポジトリアクセス、422ならrefやinputsを、応答詳細と合わせて確認してください。

## 同時実行・重複起動

既存の `concurrency.group: gashapon-monitor` と `cancel-in-progress:false` を維持し、監視ジョブを並列実行しません。実行中に複数要求が届くと、GitHub は保留中の実行を置き換える場合があります。

通信切断時には GitHub が受理済みの可能性があるため、Worker は POST を自動再試行しません。次の Cron で再び要求します。永続ロックは追加しておらず、同じ Cron の再配送などによる重複起動を完全には防止しません。同じ更新日時・店舗設定の重複通知は既存 Python の履歴で抑止します。Discord 送信後、履歴保存前に異常終了すると重複通知の可能性がある点は従来どおりです。

Cron や GitHub の実行待ちによる遅延は残るため、正確な時刻の実行や無停止を保証する仕組みではありません。

## 定期実行の停止

Cloudflare の Cron Trigger を削除します。または `wrangler.jsonc` の該当箇所を次に変更し、`npm run deploy` を実行します。

```json
"triggers": { "crons": [] }
```

ダッシュボードだけで削除すると再デプロイで復活するため、停止を維持するならファイルも更新してください。受理済み・実行中のジョブは自動で消えません。緊急時は GitHub の Workflow 無効化や実行中ジョブのキャンセルも確認します。

**`MONITOR_ENABLED=false` は外部の workflow_dispatch を止めません。** 今回は既存の条件式を維持し、schedule のみ削除しました。停止は Cloudflare で管理してください。以前の cron-job.org 等のジョブがある場合はそちらも停止します。
