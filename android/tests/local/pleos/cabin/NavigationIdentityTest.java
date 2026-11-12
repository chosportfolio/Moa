package local.pleos.cabin;

import ai.pleos.playground.navi.constants.RouteOption;
import ai.pleos.playground.navi.data.RouteInfo;

public final class NavigationIdentityTest {
    private static void check(boolean result, String message) {
        if (!result) throw new AssertionError(message);
    }
    public static void main(String[] args) {
        RouteInfo route = new RouteInfo(37.3948, 127.1112, null, null,
            "Public test station", "", "", "", RouteOption.RECOMMENDED, "", false);
        check(route.getLatitude() == 37.3948 && route.getLongitude() == 127.1112, "Actual SDK coordinate order");
        check(NavigationIdentity.nearby(37.3948, 127.1112, 37.3949, 127.1113), "Nearby entrance accepted");
        check(!NavigationIdentity.nearby(37.3948, 127.1112, 37.4048, 127.1112), "Other destination rejected");
        check(!NavigationIdentity.nearby(37.3948, 127.1112, Double.NaN, 127.1112), "Invalid coordinates rejected");
        check(!NavigationIdentity.nearby(91, 127.1112, 91, 127.1112), "Invalid latitude is not a matching destination");
        check(!NavigationIdentity.nearby(37.3948, 181, 37.3948, 181), "Invalid longitude is not a matching destination");
        check(NavigationIdentity.sameDestination(37.3948, 127.1112, "가족 카페", "",
            37.3949, 127.1113, "가족카페", "도로명 주소"), "Same POI with a nearby entrance is accepted");
        check(!NavigationIdentity.sameDestination(37.3948, 127.1112, "가족 카페", "같은 건물",
            37.3948, 127.1112, "옆집식당", "같은 건물"), "Another shop at identical coordinates must not replace the selected POI");
        check(!NavigationIdentity.sameDestination(37.3948, 127.1112, "카페", "본점 주소",
            37.3949, 127.1113, "카페", "별관 주소"), "Different known native branch identity rejected inside distance radius");
        check(!NavigationIdentity.sameDestination(37.3948, 127.1112, "카페", "",
            37.3949, 127.1113, "", ""), "Distance without any positive identity cannot confirm a pending route");
        check(NavigationIdentity.sameArrival("판교 역", "역 주소", "판교역", "역주소"), "Whitespace-normalized identity accepted");
        check(!NavigationIdentity.sameArrival("판교역", "주소", "다른역", "주소"), "Different names rejected even at same address");
        check(!NavigationIdentity.sameArrival("카페", "주소1", "카페", "주소2"), "Same-name different branches rejected");
        check(!NavigationIdentity.sameArrival("", "", "", ""), "Missing identity is never arrival");
        System.out.println("NavigationIdentity: SDK coordinate order, destination distance and arrival identity checks passed.");
    }
}
