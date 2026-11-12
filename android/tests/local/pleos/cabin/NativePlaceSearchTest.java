package local.pleos.cabin;

public final class NativePlaceSearchTest {
    private static int passed;
    private static void equal(String expected, String actual) {
        if (!expected.equals(actual)) throw new AssertionError(expected + " != " + actual);
        passed++;
    }
    private static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
    public static void main(String[] args) {
        equal("이재모피자", NativePlaceSearch.keyword("이재모피자", "부산역 도시 기준점 출발 · 40분 이내", "부산역 도시 기준점 주변 · 상세 주소 확인 필요"));
        equal("강릉시 행복칼국수", NativePlaceSearch.keyword("행복칼국수", "판교역 출발 · 120분 이내", "강릉시 경강로 100"));
        equal("인천 카페", NativePlaceSearch.keyword("카페", "인천", "상세 주소 확인 필요"));
        equal("", NativePlaceSearch.actualAddress("부산역 도시 기준점 주변 · 상세 주소 확인 필요"));
        equal("부산광역시 중구 광복중앙로 31", NativePlaceSearch.actualAddress("부산광역시 중구 광복중앙로 31"));
        equal("대전 행복칼국수", NativePlaceSearch.keyword("행복칼국수", "대전", "대전 주변 · 상세 주소 확인 필요"));
        equal("서울도시건축전시관", NativePlaceSearch.keyword("서울도시건축전시관", "서울", "서울특별시 세종대로 119"));
        equal("커피온리", NativePlaceSearch.keyword("커피온리", "현재 위치 주변", "현재 위치 주변 · 상세 주소 확인 필요"));
        equal("서울특별시 카페마마스", NativePlaceSearch.keyword("카페마마스", "현재 위치 주변", "서울특별시 무교로 16"));
        equal("강릉 행복칼국수", NativePlaceSearch.keyword("행복칼국수", "강릉 주변", ""));
        equal("강릉 행복 칼국수", NativePlaceSearch.keyword("행복\n칼국수", "강릉\t", ""));
        equal("BUONO BUONO coffee", NativePlaceSearch.keyword("BUONO　BUONO coffee", "", "상세 주소 확인 필요"));
        equal("대전 개천식당", NativePlaceSearch.keyword("개천식당 (Gaecheonsikdang)", "대전", ""));
        equal("대전 개천식당", NativePlaceSearch.keyword("개천식당(Gaecheonsikdang)", "대전", ""));
        equal("대전 개천식당", NativePlaceSearch.keyword("개천식당（Gaecheonsikdang）", "대전", ""));
        equal("서울 카페 (강남점)", NativePlaceSearch.keyword("카페 (강남점)", "서울", ""));
        equal("대전 개천식당 (본점)", NativePlaceSearch.keyword("개천식당 (Gaecheonsikdang) (본점)", "대전", ""));
        equal("서울 카페 (강남점 Gangnam)", NativePlaceSearch.keyword("카페 (강남점 Gangnam)", "서울", ""));
        equal("서울 Starbucks (Gangnam)", NativePlaceSearch.keyword("Starbucks (Gangnam)", "서울", ""));
        equal("카페", NativePlaceSearch.keyword("카페 (Café)", "", ""));
        equal("식당 (24시)", NativePlaceSearch.keyword("식당 (24시)", "", ""));
        equal("개천식당", NativePlaceSearch.keyword("개천(Gaecheon)식당", "", ""));

        NativePoiCache cache = new NativePoiCache();
        check(!cache.remember("A", 37, 127, "B", 37, 127, "poi-1", "native"), "Same coordinates cannot identify a different store");
        check(!cache.remember("A", 37, 127, "A", 38, 127, "poi-1", "native"), "Same name in another city must not match");
        check(!cache.remember("A", 37, 127, "A", 37, 127, "node/123", "OSM"), "OSM identifiers cannot open native POIs");
        check(!cache.remember("A", 37, 127, "A", 37, 127, "poi-1", ""), "Missing provider must not create a reference");
        check(cache.remember("A", 37, 127, "A", 37, 127, "poi-1", "native"), "Matching native identity is reusable");
        check(!cache.remember("A", 37, 127, "A", 37, 127, "poi-1", "native"), "Duplicate callbacks do not republish");
        check(cache.get("A", 37, 127).id.equals("poi-1"), "Known native id retained");
        check(cache.get("A", 37, 128) == null, "A different branch does not inherit the ID");
        for (int i=0;i<32;i++) cache.remember("N"+i,37,127,"N"+i,37,127,"poi-"+i,"native");
        check(cache.get("A",37,127)==null, "Oldest native reference evicted at capacity");
        System.out.println("Native place search and verified POI references: " + passed + " passed.");
    }
}
