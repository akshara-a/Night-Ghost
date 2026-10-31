# Evening Task Note

Add tests for edge cases in the payment module and clean up messy parts:
1. Write unit tests for `charge()` with negative and zero amounts, currency case normalization, and boundary decimals.
2. Write unit tests for `refund()` with invalid transaction ID formats and negative refund amounts.
3. Extract any duplicated currency rounding logic into `round_money()`.
4. Ensure all new and existing tests pass.
