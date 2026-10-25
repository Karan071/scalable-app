/** Minor-unit money formatting (every backend amount is `*_minor`, LLD-13). */
export function formatMoney(minor: number, currency = 'usd'): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(minor / 100)
}
