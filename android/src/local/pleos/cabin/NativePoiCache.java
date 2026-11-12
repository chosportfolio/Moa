package local.pleos.cabin;

import java.util.LinkedHashMap;
import java.util.Locale;
import java.text.Normalizer;
import java.util.regex.Pattern;
import java.util.regex.Matcher;

/** Bounded, process-local identities received only from public NaviHelper callbacks. */
final class NativePoiCache {
    static final int MAX_ENTRIES = 128;
    private static final Pattern NUMBER = Pattern.compile("[0-9]");
    private static final Pattern BRANCH_ANNOTATION = Pattern.compile(
        "(?i)\\([^)]*(?:\\b(?:branch|store|terminal|airport|station|outlet|dt)\\b|[0-9])[^)]*\\)");
    private static final Pattern ANNOTATION = Pattern.compile("\\(([^()]*)\\)");
    static final class Ref {
        final String id;
        final String provider;
        final String nativeName;
        final String address;
        final String source;
        final double latitude;
        final double longitude;
        Ref(String id, String provider, String nativeName, String address, String source,
                double latitude, double longitude) {
            this.id = id;
            this.provider = provider;
            this.nativeName = nativeName;
            this.address = address;
            this.source = source;
            this.latitude = latitude;
            this.longitude = longitude;
        }
    }
    private final LinkedHashMap<String, Ref> refs = new LinkedHashMap<>();

    Ref get(String name, double lat, double lon) { return get(name, "", lat, lon); }

    synchronized Ref get(String name, String address, double lat, double lon) {
        if (!NavigationIdentity.validCoordinates(lat, lon) || normalizedName(name).isEmpty()) return null;
        Ref match = null;
        for (Ref ref : refs.values()) {
            if (!sameName(name, ref.nativeName)
                    || !NavigationIdentity.nearby(lat, lon, ref.latitude, ref.longitude)
                    || addressesConflict(address, ref.address)) continue;
            // Never choose the closest of two plausible native IDs: chains and
            // separate businesses can share a building or parking entrance.
            if (match != null) return null;
            match = ref;
        }
        return match;
    }

    synchronized boolean putNative(String nativeName, double lat, double lon, String id,
            String provider, String address, String source) {
        if (!validToken(id) || !validToken(provider) || !validText(nativeName, 300)
                || !NavigationIdentity.validCoordinates(lat, lon) || (lat == 0 && lon == 0)
                || !("destination".equals(source) || "recent_destination".equals(source) || "bookmark".equals(source))) return false;
        String lowerId = id.toLowerCase(Locale.ROOT);
        if (lowerId.startsWith("node/") || lowerId.startsWith("way/") || lowerId.startsWith("relation/")) return false;
        if (address == null) address = "";
        if (address.length() > 600 || hasControl(address)) return false;
        // ID and provider are opaque. Length-prefixing avoids delimiter collisions.
        String key = provider.length() + ":" + provider + id;
        Ref prior = refs.get(key);
        if (prior != null && prior.nativeName.equals(nativeName) && prior.address.equals(address)
                && prior.latitude == lat && prior.longitude == lon) return false;
        refs.remove(key);
        refs.put(key, new Ref(id, provider, nativeName, address, source, lat, lon));
        while (refs.size() > MAX_ENTRIES) refs.remove(refs.keySet().iterator().next());
        return true;
    }

    boolean remember(String name, double lat, double lon, String nativeName, double nativeLat,
            double nativeLon, String id, String provider) {
        if (!sameName(name, nativeName) || !NavigationIdentity.nearby(lat, lon, nativeLat, nativeLon)) return false;
        return putNative(nativeName, nativeLat, nativeLon, id, provider, "", "destination");
    }

    private static boolean validToken(String value) {
        return validText(value, 200);
    }

    private static boolean validText(String value, int limit) {
        return value != null && !value.trim().isEmpty() && value.length() <= limit && !hasControl(value);
    }

    private static boolean hasControl(String value) {
        for (int i = 0; i < value.length(); i++) if (Character.isISOControl(value.charAt(i))) return true;
        return false;
    }

    private static String normalizedName(String value) {
        return normalized(NativePlaceSearch.canonicalName(value));
    }

    private static String normalized(String value) {
        return value == null ? "" : Normalizer.normalize(value, Normalizer.Form.NFKC)
            .toLowerCase(Locale.ROOT).replaceAll("[^\\p{L}\\p{N}]", "");
    }

    private static boolean sameName(String expected, String actual) {
        if (normalized(expected).equals(normalized(actual)) && !normalized(expected).isEmpty()) return true;
        // Don't reinterpret an English branch/terminal label as romanization.
        if (BRANCH_ANNOTATION.matcher(Normalizer.normalize(expected == null ? "" : expected, Normalizer.Form.NFKC)).find()
                || BRANCH_ANNOTATION.matcher(Normalizer.normalize(actual == null ? "" : actual, Normalizer.Form.NFKC)).find()) return false;
        String left = normalizedName(expected);
        return !left.isEmpty() && left.equals(normalizedName(actual))
            && safeAliasRemoval(expected) && safeAliasRemoval(actual);
    }

    private static boolean safeAliasRemoval(String name) {
        if (name == null) return false;
        String input = Normalizer.normalize(name, Normalizer.Form.NFKC);
        String canonical = NativePlaceSearch.canonicalName(input);
        if (normalized(input).equals(normalized(canonical))) return true;
        String spelling = latinSpelling(canonical);
        Matcher annotations = ANNOTATION.matcher(input);
        while (annotations.find()) {
            String inside = annotations.group(1);
            // Retained Korean annotations are still part of the exact name.
            if (canonical.contains(annotations.group())) continue;
            String alias = normalized(inside);
            // Conservative: a Latin translation, branch such as (Gangnam), or
            // uncertain spelling falls back to search. It never assigns an ID.
            if (alias.length() < 3 || !spelling.contains(alias)) return false;
        }
        return true;
    }

    private static String latinSpelling(String text) {
        // A deliberately narrow syllable spelling check, not a Korean name
        // translator. Sound changes/brand spellings that differ are left unmatched.
        String[] initials = {"g","kk","n","d","tt","r","m","b","pp","s","ss","","j","jj","ch","k","t","p","h"};
        String[] vowels = {"a","ae","ya","yae","eo","e","yeo","ye","o","wa","wae","oe","yo","u","wo","we","wi","yu","eu","ui","i"};
        String[] finals = {"","k","k","k","n","n","n","t","l","k","m","l","l","l","p","l","m","p","p","t","t","ng","t","t","k","t","p","t"};
        StringBuilder result = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char value = text.charAt(i);
            if (value >= 0xac00 && value <= 0xd7a3) {
                int syllable = value - 0xac00;
                result.append(initials[syllable / 588]).append(vowels[(syllable % 588) / 28]).append(finals[syllable % 28]);
            } else result.append(value);
        }
        return normalized(result.toString());
    }

    private static boolean addressesConflict(String expected, String actual) {
        String left = streetAddress(expected), right = streetAddress(actual);
        if (left.isEmpty() || right.isEmpty()) return false;
        return !left.equals(right) && !left.endsWith(right) && !right.endsWith(left);
    }

    private static String streetAddress(String value) {
        if (value == null) return "";
        value = Normalizer.normalize(value, Normalizer.Form.NFKC);
        if (value.contains("확인 필요") || value.contains("현재 위치")
                || value.contains("현위치") || value.contains("주변") || value.contains("미정")
                || !NUMBER.matcher(value).find()) return "";
        String address = value.replace("서울특별시", "서울").replace("부산광역시", "부산")
            .replace("대구광역시", "대구").replace("인천광역시", "인천").replace("광주광역시", "광주")
            .replace("대전광역시", "대전").replace("울산광역시", "울산").replace("세종특별자치시", "세종")
            .replace("경기도", "경기").replace("충청북도", "충북").replace("충청남도", "충남")
            .replace("전라북도", "전북").replace("전북특별자치도", "전북").replace("전라남도", "전남")
            .replace("경상북도", "경북").replace("경상남도", "경남").replace("강원특별자치도", "강원")
            .replace("강원도", "강원").replace("제주특별자치도", "제주");
        // Preserve a parcel-number separator: 123-4 is not parcel 1234.
        return Normalizer.normalize(address, Normalizer.Form.NFKC).toLowerCase(Locale.ROOT)
            .replaceAll("[^\\p{L}\\p{N}-]", "");
    }
}
