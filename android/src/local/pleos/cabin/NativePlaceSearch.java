package local.pleos.cabin;

import java.text.Normalizer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Search only actual place terms; presentation text is never a POI keyword. */
final class NativePlaceSearch {
    private NativePlaceSearch() { }
    private static final Pattern PARENTHESES = Pattern.compile("\\(([^()]*)\\)");
    private static final Pattern HANGUL = Pattern.compile("\\p{IsHangul}");
    private static final Pattern LATIN = Pattern.compile("\\p{IsLatin}");

    static String keyword(String name, String region, String address) {
        String place = canonicalName(name);
        String area = clean(region);
        if (presentationText(area)) area = "";
        area = area.replaceFirst("\\s*(?:주변|근처)$", "").trim();
        if (area.isEmpty()) {
            String location = actualAddress(address);
            if (!location.isEmpty()) {
                // Full street addresses plus a store name are too restrictive.
                // The city/district qualifies a chain without duplicating its address.
                area = location.split(" ")[0];
            }
        }
        if (area.isEmpty() || NavigationIdentity.normalized(place).contains(NavigationIdentity.normalized(area))) return place;
        return area + " " + place;
    }

    static String canonicalName(String name) { return withoutRomanization(clean(name)); }

    static String actualAddress(String address) {
        String value = clean(address);
        // Recommendation fallback labels are display copy, never a store address.
        return presentationText(value) ? "" : value;
    }

    private static boolean presentationText(String text) {
        return placeholder(text) || text.contains("기준점") || text.contains("출발")
            || text.contains("이내") || text.contains("검색 중심") || text.contains("여러 지역")
            || text.contains("기준 ·") || text.contains("실시간 교통") || text.contains("미반영")
            || text.matches(".*\\d+\\s*(?:분|시간).*" );
    }

    private static String withoutRomanization(String name) {
        Matcher annotation = PARENTHESES.matcher(name);
        StringBuffer result = new StringBuffer();
        while (annotation.find()) {
            String inside = annotation.group(1);
            String outside = name.substring(0, annotation.start()) + name.substring(annotation.end());
            boolean latinOnly = LATIN.matcher(inside).find();
            for (int offset = 0; latinOnly && offset < inside.length();) {
                int character = inside.codePointAt(offset);
                if (Character.isLetter(character) && Character.UnicodeScript.of(character) != Character.UnicodeScript.LATIN)
                    latinOnly = false;
                offset += Character.charCount(character);
            }
            // OSM often appends an English transliteration that the native POI
            // catalogue does not index. Keep Korean branch labels and purely
            // English store names intact; change only the outgoing query.
            boolean remove = latinOnly && HANGUL.matcher(outside).find();
            annotation.appendReplacement(result, remove ? "" : Matcher.quoteReplacement(annotation.group()));
        }
        annotation.appendTail(result);
        return clean(result.toString());
    }

    private static boolean placeholder(String text) {
        return text.isEmpty() || text.contains("현재 위치") || text.contains("현위치")
            || text.contains("검색 지역") || text.contains("확인 필요") || text.contains("대화 내용")
            || text.equals("주변") || text.equals("근처") || text.equals("미정");
    }

    private static String clean(String value) {
        if (value == null) return "";
        return Normalizer.normalize(value, Normalizer.Form.NFKC)
            .replaceAll("[\\p{Cc}\\p{Cf}]", " ").replaceAll("\\s+", " ").trim();
    }
}
