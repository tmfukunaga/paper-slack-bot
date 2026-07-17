# Paper Slack Bot

## まずここだけ編集します

通常の調整は **`config.yaml`だけ**で行います。Pythonコードを編集する必要はありません。

GitHubで次の順に開きます。

```text
Code
→ config.yaml
→ 右上の鉛筆アイコン（Edit this file）
→ 内容を編集
→ Commit changes...
→ Commit directly to the main branch
```

編集後は次の順で動作確認します。

```text
Actions
→ Paper Watch
→ Run workflow
```

### 1. 重要キーワードを編集する

`config.yaml`の以下を編集します。

```yaml
keywords:
  core:
    terms:
      - molecular nanotube
      - carbon nanobelt

  strong:
    terms:
      - macrocycle
      - supramolecular
```

- `core`：特に重要なキーワード
- `strong`：関連性の高いキーワード
- 1行につき1語句を追加・削除します
- `CPP`を含め、キーワードごとの特別処理はありません。最終スコアで採否を決めます

キーワードの点数も同じ場所で変更できます。

```yaml
title_score: 10
abstract_score: 5
title_cap: 20
abstract_cap: 15
```

### 2. ドボンキーワードを編集する

`exclusion_keywords.terms`を編集します。

```yaml
exclusion_keywords:
  terms:
    - peptide
    - protein
    - drug delivery
```

減点値も同じ場所で変更できます。

```yaml
title_penalty: 12
abstract_penalty: 8
title_cap: 24
abstract_cap: 16
```

### 3. ジャーナルTierを編集する

`journal_tiers`の`tier_s`、`tier_a`、`tier_b`を編集します。

```yaml
journal_tiers:
  tier_a:
    score: 7
    journals:
      - canonical: ChemRxiv
        aliases:
          - ChemRxiv
          - ChemRxiv Preprints
```

- `canonical`：Slackに表示される標準名
- `aliases`：OpenAlexで別表記になった場合の候補
- 同じジャーナルや別名を複数Tierへ重複登録すると、テストが失敗して知らせます
- `ChemRxiv`はTier Aに登録済みです

### 4. 投稿閾値を編集する

```yaml
posting:
  minimum_total_score: 15
  minimum_keyword_score: 3
```

判定式は次のとおりです。

```text
Total score = keyword score + journal score - exclusion penalty
```

投稿には、`minimum_total_score`と`minimum_keyword_score`の両方を満たす必要があります。

### YAML編集時の注意

- インデントには半角スペースを使い、タブは使わないでください
- `-`を消さないでください
- `:`を含む文章は引用符で囲んでください
- 設定ミス、重複キーワード、ジャーナルのTier重複はGitHub Actionsのテストで検出されます

---

## 動作概要

3時間ごとにOpenAlexを検索し、設定したキーワード、ジャーナルTier、ドボンキーワードで採点して、条件を満たす未投稿論文をSlackへ投稿します。1日30報は目安であり、件数上限ではありません。

Slack投稿の順番は次のとおりです。

1. タイトル、著者、ジャーナル、公開日、Score
2. Matched keywords
3. 論文ページから取得した画像（取得成功時のみ）
4. 「論文を開く」ボタン
5. Abstract
6. 論文間を区切る横線

日本語翻訳は行いません。

## 論文画像の取得

画像は次の順で探索します。

1. Graphical Abstract / TOC graphic用メタデータ
2. `og:image`
3. `twitter:image`
4. JSON-LDの代表画像
5. ページ内のGraphical Abstract、Figure、Scheme、Hero画像候補
6. ページ内で大きく表示される画像
7. 最終手段として論文ページ上部のスクリーンショット

ロゴ、favicon、広告、プロフィール画像、雑誌表紙らしい画像は可能な範囲で除外します。出版社によるアクセス拒否やCAPTCHAなどで取得できない場合は、画像なしで論文を投稿します。

取得画像はBotがSlackへアップロードし、投稿内に表示します。外部画像URLのホットリンクには依存しません。

## 必要なアカウント

- Slack workspace
- GitHub account
- OpenAlex account/API key

OpenAI APIや翻訳APIは使用しません。

## GitHub Secrets

Repositoryで次を開きます。

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

次の4件を登録します。

| Secret名 | 内容 |
|---|---|
| `OPENALEX_API_KEY` | OpenAlex API key |
| `SLACK_BOT_TOKEN` | `xoxb-...`で始まるBot token |
| `SLACK_CHANNEL_ID` | 投稿先チャンネルの`C...` |
| `CONTACT_EMAIL` | OpenAlex/Crossrefへの連絡先メールアドレス |

## Slack App設定

Bot Token Scopesに次を追加します。

```text
chat:write
files:write
```

`files:write`追加後は、AppをWorkspaceへ再インストールし、表示されたBot User OAuth TokenをGitHub Secret `SLACK_BOT_TOKEN`へ登録し直してください。

投稿先チャンネルでは次を実行します。

```text
/invite @Paper Watch
```

## 初回実行

```text
Actions
→ Paper Watch
→ Run workflow
```

初回はPlaywright用Chromiumを準備するため、通常より時間がかかります。以後はGitHub Actionsのキャッシュを利用します。

## 定期実行

`.github/workflows/paper-watch.yml`は3時間ごとに実行されます。

```yaml
- cron: "17 */3 * * *"
```

cronはUTCです。

## 投稿済み論文の記録

`data/state.json`に投稿済みDOIを保存し、重複投稿を防ぎます。

更新用ZIPを既存Repositoryへ反映するときは、現在の`data/state.json`を削除・上書きしないでください。

## 手動テスト

GitHub Actionsでは投稿前に次を実行します。

```text
pytest -q
```

テストが失敗した場合はSlack投稿を開始しません。
