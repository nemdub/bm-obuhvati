/**
 * UI strings, authored in Serbian Cyrillic (single source). For the Latin script they
 * are transliterated mechanically via translit.ts, so there is only one set to maintain.
 */
import { tr, type Script } from "./translit";

export const STRINGS = {
  appTitle: "Бирачка места — обухвати",
  appSubtitle: "Провера и исправка обухвата бирачких места",
  municipalities: "Општине",
  municipality: "Општина",
  stations: "Бирачка места",
  station: "Бирачко место",
  number: "Број",
  name: "Назив",
  address: "Адреса",
  coverage: "Обухват",
  segments: "Сегменти обухвата",
  rawText: "Изворни текст",
  needsReview: "За проверу",
  reviewReason: "Разлог за проверу",
  reviewed: "Проверено",
  street: "Улица",
  streetUnresolved: "Улица није пронађена",
  wholeStreet: "Цела улица",
  ranges: "Распони",
  parityAll: "сви",
  parityOdd: "непарни",
  parityEven: "парни",
  singles: "Појединачни бројеви",
  confidence: "Поузданост",
  amendmentNote: "Измена решења",
  save: "Сачувај",
  revert: "Врати аутоматско",
  markReviewed: "Означи као проверено",
  recompute: "Затражи поновни прорачун",
  recomputeQueued: "Затражен поновни прорачун полигона",
  points: "Адресе",
  polygon: "Полигон",
  matchedAddresses: "Повезане адресе",
  noPointsForStreet: "Нема повезаних адреса за ову улицу",
  noPolygon: "Нема полигона",
  back: "Назад",
  total: "Укупно",
  addRange: "Додај распон",
  addSingle: "Додај број",
  suffix: "Суфикс",
  saved: "Сачувано",
  script: "Писмо",
  latin: "Latinica",
  cyrillic: "Ћирилица",
  stale: "Полигон је застарео (има ручних измена)",
  source: "Извор",
  base: "основно",
  amendment: "измена",
} as const;

// Why a segment is flagged for review, keyed by the pipeline's reason codes (Cyrillic
// source; transliterated for the Latin script like all other UI text).
export const REVIEW_REASONS: Record<string, string> = {
  street_unresolved: "Улица није пронађена у регистру",
  fuzzy: "Улица пронађена приближним подударањем имена",
  muni_fallback: "Улица је у другом насељу, не у насељу бирачког места",
  unknown_tokens: "Има непрепознатих бројева или ознака у тексту",
  named_block: "Именовани блок — потребно ручно повезивање",
  unknown_kind: "Непознат тип сегмента",
  amendment: "Унето из измене или допуне решења",
  no_match: "Распони или бројеви не одговарају ниједној адреси у регистру",
  conflict: "Адресе се преклапају са другим бирачким местом",
  parity_unconfirmed: "Претпостављена парна/непарна страна није потврђена другим бирачким местом",
};

export type StringKey = keyof typeof STRINGS;

/** Translate a UI key into the active script. */
export function makeT(script: Script) {
  return (key: StringKey) => tr(STRINGS[key], script);
}
