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

/**
 * Serbian Cyrillic (azbuka) collation. The letters Ђ, Ј, Љ, Њ, Ћ, Џ sit in a different
 * Unicode block than basic Cyrillic, so code-point order is wrong; this ranks by azbuka:
 *   а б в г д ђ е ж з и ј к л љ м н њ о п р с т ћ у ф х ц ч џ ш
 * (љ, њ, џ are single code points in Cyrillic — no digraph handling needed).
 */
const AZBUKA = [
  "а", "б", "в", "г", "д", "ђ", "е", "ж", "з", "и", "ј", "к", "л", "љ", "м",
  "н", "њ", "о", "п", "р", "с", "т", "ћ", "у", "ф", "х", "ц", "ч", "џ", "ш",
];
const CYR_RANK = new Map<string, number>(AZBUKA.map((l, i) => [l, 10 + i]));

function srCyrillicKey(input: string): string {
  const s = input.toLowerCase();
  const codes: number[] = [];
  for (const ch of s) {
    if (CYR_RANK.has(ch)) codes.push(CYR_RANK.get(ch)!);
    else if (ch === " " || ch === "-" || ch === ".") codes.push(2);
    else codes.push(200 + ch.charCodeAt(0));
  }
  return String.fromCharCode(...codes);
}

export function srCyrillicCompare(a: string, b: string): number {
  const ka = srCyrillicKey(a);
  const kb = srCyrillicKey(b);
  return ka < kb ? -1 : ka > kb ? 1 : 0;
}
