name: backtest

on:
  workflow_dispatch:
    inputs:
      rebuild_prices:
        description: "일봉을 1500거래일로 다시 받기 (첫 실행에는 반드시 체크)"
        type: boolean
        default: false

permissions:
  contents: write

jobs:
  backtest:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      - uses: actions/cache@v4
        with:
          path: data
          key: prices-${{ github.run_id }}
          restore-keys: prices-

      # 백테스트에는 448일선 워밍업 뒤로 충분한 구간이 필요하다.
      # 520거래일이면 검증 구간이 72일뿐이라 의미가 없어 1500거래일로 늘린다.
      - name: 일봉 확장 수집
        if: ${{ inputs.rebuild_prices }}
        env:
          DART_API_KEY: ${{ secrets.DART_API_KEY }}
        run: python scripts/prices.py --data data --days 1500 --rebuild --budget-min 70

      - name: 백테스트
        run: python scripts/backtest.py --data data --out docs/backtest.json --max-hold 60

      - name: 결과 커밋
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add docs/backtest.json
          if git diff --staged --quiet; then
            echo "변경 없음"
          else
            git commit -m "backtest: $(date -u +%Y-%m-%d)"
            git push
          fi
