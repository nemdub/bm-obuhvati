/**
 * Serbian Cyrillic -> Latin transliteration.
 *
 * The whole UI is authored in Cyrillic (the single source of truth); the Latin script
 * is produced mechanically on render. Register names/addresses are already stored in
 * both scripts, so this is used for UI chrome and free-text fields (coverage, notes).
 */

const CYR_TO_LAT: Record<string, string> = {
  А: "A", Б: "B", В: "V", Г: "G", Д: "D", Ђ: "Đ", Е: "E", Ж: "Ž", З: "Z", И: "I",
  Ј: "J", К: "K", Л: "L", Љ: "Lj", М: "M", Н: "N", Њ: "Nj", О: "O", П: "P", Р: "R",
  С: "S", Т: "T", Ћ: "Ć", У: "U", Ф: "F", Х: "H", Ц: "C", Ч: "Č", Џ: "Dž", Ш: "Š",
  а: "a", б: "b", в: "v", г: "g", д: "d", ђ: "đ", е: "e", ж: "ž", з: "z", и: "i",
  ј: "j", к: "k", л: "l", љ: "lj", м: "m", н: "n", њ: "nj", о: "o", п: "p", р: "r",
  с: "s", т: "t", ћ: "ć", у: "u", ф: "f", х: "h", ц: "c", ч: "č", џ: "dž", ш: "š",
};

export function cyrToLat(s: string): string {
  let out = "";
  for (const ch of s) out += CYR_TO_LAT[ch] ?? ch;
  return out;
}

export type Script = "cyr" | "lat";

/** Transliterate Cyrillic source text for the requested script. */
export function tr(text: string | null | undefined, script: Script): string {
  if (!text) return "";
  return script === "lat" ? cyrToLat(text) : text;
}
