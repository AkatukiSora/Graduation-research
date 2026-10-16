# 名古屋 rule_test 一時データセット

このbundleは `nagoya-rule-test-202602-v1` というテスト専用のimmutable版です。JARTICの2026年02月分に由来する地域内実在地点を正規化し、欠損方向へ最近傍地点proxyを付けています。`synthetic=true` は座標がすべて架空という意味ではなく、テスト用に生成した方向値を含むデータであることを示します。

方向の意味は `stop_to_neighbor_proxy`（停止地点から最近傍の別停止地点への方位）です。実道路の進行方向、車両の接近方向、標識面の向き、規制の現在有効性を示しません。confirmedは含まず、有効性はすべてunknownです。推定精度・現地確認・安全運転通知への適合性は保証しません。

## 出典・加工・利用条件

交通規制情報（公益財団法人日本道路交通情報センター、2026年02月分）を本プロジェクトが地域抽出・正規化し、近傍点proxyを付与しました。推定値は原提供者が作成・確認したものではありません。提供元案内: https://www.jartic.or.jp/service/opendata/ 。参照した現行利用条件: https://www.jartic.or.jp/d/opendata/riyou_kiyaku.pdf （2026年10月14日参照）。保存原本の取得日時と取得時に適用された規約は不明です。現在参照する規約は過去の取得条件の証拠ではありません。公開責任者の受容判断は法的保証を意味しません。

## 生成・検証

1. 保存原本のSHA-256が `f12388fca9283f0abd9104a3b265f88caac56f43a9add92404d22a34de0e6ea5` と一致し、member `typeD_aichi/愛知県警_202602_k_2.1.csv` のSHA-256が `0d902b472980d40fa7b472998e3424c73ad4730108fac9655ac1b23e82ee573d` と一致することを確認します。ZIP全体を展開せず、このmemberだけをprivate data directoryへ逐次展開し、元ZIP/CSVをGitへ追加しません。
2. 固定B1境界（中心 35.1202166, 136.9121477、球面半径2000m、360頂点、9桁、`covers`）を用いて既存 `generation_io prepare-input` と `generate` を実行し、schema 3 `generation-manifest.json` をverifyします。source CSV SHA、boundary SHA、input manifest SHAは生成物から記録されます。
3. lock環境と単一threadを固定して実行します。

```sh
cd backend/generation_io
uv sync --locked --all-groups
export PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
python -m generation_io.rule_test_seed build --generation-manifest <verified-schema3-generation-manifest> --source-zip <private-path-to-typeD_aichi(1).zip> --dataset-id nagoya-rule-test-202602-v1 --notice-config <private-notice-config.json> --output-dir ../app/internal/seeddata/data/nagoya-rule-test-202602-v1
python -m generation_io.rule_test_seed verify --bundle ../app/internal/seeddata/data/nagoya-rule-test-202602-v1
```

`REPORT.json` は入力件数、coverage、状態・理由件数、digestを記録します。manifestとJSONL shardは決定的に生成されます。同じ入力・lock環境で別の新規output directoryへ再生成し、全ファイルのSHA-256が一致することを確認してください。

## 一時データの撤去・置換

本番用データが完成したら、この一時datasetを本番へ昇格させず、新しいdataset IDのimmutable版を追加して置き換えます。追加投入、API selector切替、rollback、任意退役は別操作です。確認手順と退役時の人手ゲートは [`backend/app/db/seed/README.md` の「immutable dataset の切替・退役手順`](../../../../../db/seed/README.md#immutable-dataset-の切替退役手順) を参照してください。全reader停止、seed設定からのID除去、backup/rollback期間、archive保全を人が確認するまで削除しません。
