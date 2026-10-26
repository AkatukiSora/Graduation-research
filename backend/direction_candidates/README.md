# 方向推定のレビュー用データ

`c1r1-kansai-chubu-v1/` は、2026年10月24日に生成した一時停止地点の方向推定結果を保存するテスト・レビュー専用bundleです。本番データ完成後に置き換えるための一時データであり、本番承認済みではありません（`production_approved=false`）。実在地点を含み、安全運転通知や実運転の判断には利用できません。

## 内容と方向の意味

| ファイル | 全行 | 推定あり | 推定不能 |
|---|---:|---:|---:|
| `teacher-directions.jsonl`（滋賀・28セル） | 394 | 346 | 48 |
| `target-directions.jsonl`（名古屋・4セル） | 1939 | 1859 | 80 |

全2333行に `confidence`（有限な0〜1）があります。`direction_deg` は北を0°とする時計回りの、交差点へ向かう車両進行方向です。標識の表面が向く方向や規制の有効性を示しません。推定不能の128行は `direction_deg=null`、`confidence=0`、`status=no_usable_road_candidate` です。推定ありでもconfidenceが0の場合があります。

confidenceは正解確率ではなく未較正のスコアです。位置揺らぎに対する候補選択安定性をJ、道路距離(m)をd、10/25/40m接線の最大角度差(°)をsとして、25m接線は `J × exp(-d/20) × exp(-s/45)`、局所10m接線へのfallbackは `J × exp(-d/20) × 0.5` で算出しました。`hard_gate_pass` と `hard_gate_reasons` は診断情報であり、不通過の行も含まれます。confidenceによる採否閾値は設定していません。RFモデルは使用していません。

teacherの推定346行について、既知の入口座標→標識座標を参照した±20°正解は258行（74.57%）です。推定不能を含む394行に対しては65.48%です。teacherは固定選択で無作為標本・最終holdoutではなく、targetの精度は未測定です。人間による全件の目視レビューや精度80〜90%の達成を示しません。manifestの評価値は生成時の記録であり、このbundleの検証では精度を再計算しません。

## 既存seedとの関係

このディレクトリは `backend/app/internal/seeddata` の外にあり、Goバイナリへの埋め込み・DB投入・API応答には接続していません。このbundleのIDを `SEED_DATASET_IDS` や `STOP_POINT_DATASET_ID` に設定しないでください。既存の `synthetic-v1` / `synthetic-v2` と `nagoya-rule-test-202602-v1` は変更しません。特に、近傍地点proxyを使う既存の名古屋seedと、本bundleの道路接線由来の推定方向は別の値です。

本番利用には別途、出自・権利・方向の正しさ・有効性・通知適合性の確認と、明示的な承認、DB/APIへの実装が必要です。本番用データへの昇格操作はありません。置換後に不要となったら、このレビュー用ディレクトリと専用workflowを削除できます。DBやAPI selectorの切替は発生しません。

## 出典と加工

地点は公益財団法人日本道路交通情報センター（JARTIC）の2026年02月分交通規制情報に由来します。地域・セルを抽出し、OpenStreetMapの道路候補を用いて本プロジェクトが方向とconfidenceを計算しました。推定値は原提供者が作成・確認したものではありません。元CSVのSHA-256と使用したPBF snapshot名はmanifestに記録しています。

- JARTIC提供元: https://www.jartic.or.jp/service/opendata/
- JARTIC利用規約: https://www.jartic.or.jp/d/opendata/riyou_kiyaku.pdf
- © OpenStreetMap contributors: https://www.openstreetmap.org/copyright
- OpenStreetMap由来の道路データは Open Database License (ODbL) に基づきます: https://opendatacommons.org/licenses/odbl/1-0/

保存原本の取得日時と取得時に適用されたJARTIC規約は不明です。現在の案内は過去の取得条件の証拠ではなく、このテスト用公開は法的保証や第三者による承認を意味しません。

## 検証と再現範囲

リポジトリルートでPython 3（標準ライブラリのみ）を使用します。

```sh
python3 backend/direction_candidates/validate.py
python3 -m unittest discover -s backend/direction_candidates -p 'test_*.py' -v
cd backend/app
go test ./internal/seed/... ./internal/seeddata/...
go run ./cmd/seed --validate-only
```

validatorはファイルのSHA-256、件数、全体でのID一意性、座標・confidence・方向角の範囲、推定不能のnull表現、source CSV digest、未承認フラグを検証します。CIは同じ検証と既存seedのDB不要テストを実行します。ライブDB、query統合テスト、再学習、PBF走査は実行しません。

JSONLは生成済み結果をバイト無変更で保存したもので、変換・並べ替えはありません。再取得した同名のCSV/PBFで一致を保証するものではありません。元の準備済み入力・道路走査環境は同梱しておらず、このリポジトリ単体で推論を再生成することはできません。manifestのSHA-256は結果の同一性を確認するもので、原本からの出自や現地の正しさを独立に証明しません。
