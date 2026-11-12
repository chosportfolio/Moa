package local.pleos.cabin;

public final class NativePoiCacheTest {
    private static int passed;
    private static void check(boolean value, String message) {
        if (!value) throw new AssertionError(message);
        passed++;
    }
    private static NativePoiCache one(String name, String address) {
        NativePoiCache cache = new NativePoiCache();
        check(cache.putNative(name, 36.33, 127.43, "native-id", "native-provider", address,
            "recent_destination"), "Public callback identity accepted");
        return cache;
    }
    public static void main(String[] args) {
        NativePoiCache cache = one("개천식당", "대전광역시 동구 대전로 779");
        NativePoiCache.Ref ref = cache.get("개천식당 (Gaecheonsikdang)",
            "대전 동구 대전로779", 36.3301, 127.4301);
        check(ref != null && ref.id.equals("native-id"), "Actual native identity matches Korean name with Latin alias");
        check(ref.nativeName.equals("개천식당") && ref.address.equals("대전광역시 동구 대전로 779")
            && ref.source.equals("recent_destination"), "Canonical callback values retained without rewriting");
        check(cache.get("개천식당", "대전 주변 · 상세 주소 확인 필요", 36.33, 127.43) != null,
            "Placeholder is not a conflicting address");
        check(cache.get("개천식당", 36.332, 127.43) == null, "Matching name outside 150m rejected");
        check(cache.get("다른식당", 36.33, 127.43) == null, "Neighbor at identical coordinates rejected");
        check(cache.get("개천식당", "대전 동구 대전로 780", 36.33, 127.43) == null,
            "Known different address cannot inherit a native identity");
        check(cache.get("개천식당", "대전 동구 대전로 ７８０", 36.33, 127.43) == null,
            "Full-width address digits cannot bypass the address conflict check");
        check(cache.get("개천식당", Double.NaN, 127.43) == null, "Invalid coordinates rejected");
        cache.putNative("개천식당", 36.3302, 127.43, "other-native-id", "native-provider", "대전 동구 대전로 781", "bookmark");
        check(cache.get("개천식당", 36.33, 127.43) == null, "Two plausible native identities remain ambiguous");
        check(cache.get("개천식당", "대전 동구 대전로779", 36.33, 127.43) != null,
            "Actual street address resolves same-name ambiguity");

        NativePoiCache branches = one("행복카페 (강남점)", "");
        check(branches.get("행복카페 강남점", 36.33, 127.43) != null, "Parenthesis formatting does not erase branch text");
        check(branches.get("행복카페 (역삼점)", 36.33, 127.43) == null, "Nearby different Korean branch rejected");
        check(branches.get("행복카페", 36.33, 127.43) == null, "Unqualified chain name is not a branch identity");
        NativePoiCache terminals = one("공항카페", "");
        check(terminals.get("공항카페 (Terminal 2)", 36.33, 127.43) == null,
            "English terminal annotation must not be removed as romanization");
        check(terminals.get("공항카페 (DT)", 36.33, 127.43) == null, "English branch qualifier retained");
        check(terminals.get("공항카페 (Gangnam)", 36.33, 127.43) == null,
            "Bare English neighborhood cannot be stripped into a different native branch");
        check(cache.get("개천(Gaecheon)식당", "대전 동구 대전로779", 36.33, 127.43) != null,
            "A conservative embedded romanization remains matchable");

        NativePoiCache parcels = one("식당", "충청북도 충주시 금릉동 123-4");
        check(parcels.get("식당", "충북 충주시 금릉동 1234", 36.33, 127.43) == null,
            "Different parcel numbers cannot collide after normalization");
        NativePoiCache floors = one("식당", "충북 충주시 금릉로 30 2층");
        check(floors.get("식당", "충북 충주시 금릉로 30 3층", 36.33, 127.43) == null,
            "Known floor conflict rejected");

        NativePoiCache bounded = new NativePoiCache();
        check(!bounded.putNative("가게", 36, 127, "way/234", "native", "", "bookmark"), "OSM ID rejected");
        check(!bounded.putNative("가게", 36, 127, "id", "", "", "bookmark"), "Provider required");
        check(!bounded.putNative("가게", 0, 0, "id", "native", "", "bookmark"), "Missing native location rejected");
        check(!bounded.putNative("가게", 36, 127, "id\n", "native", "", "bookmark"), "Control character rejected");
        check(!bounded.putNative("가게", 36, 127, "id", "native", "", "external_search"), "Source limited to public native callbacks");
        check(bounded.putNative("원본", 36, 127, "original", "native", "", "destination"), "Destination callback supported");
        check(!bounded.putNative("원본", 36, 127, "original", "native", "", "bookmark"), "Repeated same native identity is deduplicated");
        check(bounded.get("원본", 36, 127) != null, "Repeated callbacks do not create ambiguous entries");
        for (int i = 0; i < NativePoiCache.MAX_ENTRIES; i++) {
            bounded.putNative("이름" + i, 36, 127, "native-" + i, "native", "", "recent_destination");
        }
        check(bounded.get("원본", 36, 127) == null, "Oldest entry evicted at bounded capacity");
        check(bounded.get("이름127", 36, 127) != null, "Newest entry retained");
        bounded.putNative("새이름", 36, 127, "native-127", "native", "", "bookmark");
        check(bounded.get("이름127", 36, 127) == null && bounded.get("새이름", 36, 127) != null,
            "Updated canonical identity replaces old spelling for the same native ID");
        System.out.println("Native POI matching: " + passed + " passed.");
    }
}
