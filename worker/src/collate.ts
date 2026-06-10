/**
 * Serbian Latin (abeceda) collation.
 *
 * SQLite/JS default string ordering sorts by Unicode code point, which places the Serbian
 * letters Č, Ć, Đ, Š, Ž after Z and ignores the digraphs Dž, Lj, Nj. This builds a sort
 * key in correct abeceda order:
 *   a b c č ć d dž đ e f g h i j k l lj m n nj o p r s š t u v z ž
 */
const ABECEDA = [
  "a", "b", "c", "č", "ć", "d", "dž", "đ", "e", "f", "g", "h", "i", "j", "k",
  "l", "lj", "m", "n", "nj", "o", "p", "r", "s", "š", "t", "u", "v", "z", "ž",
];
const DIGRAPHS = ["dž", "lj", "nj"];
const RANK = new Map<string, number>(ABECEDA.map((l, i) => [l, 10 + i]));

function srLatinKey(input: string): string {
  const s = input.toLowerCase();
  const codes: number[] = [];
  for (let i = 0; i < s.length; ) {
    const two = s.slice(i, i + 2);
    if (DIGRAPHS.includes(two)) {
      codes.push(RANK.get(two)!);
      i += 2;
      continue;
    }
    const ch = s[i];
    i += 1;
    if (RANK.has(ch)) codes.push(RANK.get(ch)!);
    else if (ch === " " || ch === "-" || ch === ".") codes.push(2); // separators sort first
    else codes.push(200 + ch.charCodeAt(0)); // unknown chars after letters, deterministic
  }
  return String.fromCharCode(...codes);
}

export function srLatinCompare(a: string, b: string): number {
  const ka = srLatinKey(a);
  const kb = srLatinKey(b);
  return ka < kb ? -1 : ka > kb ? 1 : 0;
}
