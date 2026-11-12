"""Fresh, memory-only positions. Windows input is a development adapter only."""
from datetime import datetime, timezone
import math
import threading
import time


class LocationUnavailable(ValueError):
    pass


class HostPosition:
    MAX_AGE = 120
    MAX_ACCURACY = 1000
    SOURCES = {'WiFi', 'Satellite', 'Cellular'}
    REQUIRE_ACCURACY = True

    def __init__(self, *, utc_now=None, monotonic=None):
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._position = None
        self._received = None
        self._initial_age = 0
        self._invalid_status = None

    def update(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('현재 위치 정보를 확인해 주세요.')
        def number(key, low, high):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('현재 위치 정보를 확인해 주세요.')
            return float(value)
        latitude = number('latitude', -90, 90)
        longitude = number('longitude', -180, 180)
        accuracy = number('accuracy_m', 0.01, self.MAX_ACCURACY) if self.REQUIRE_ACCURACY or payload.get('accuracy_m') is not None else None
        confidence = payload.get('confidence')
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence)):
            raise ValueError('위치 측정 정보를 확인해 주세요.')
        source = payload.get('source')
        if source not in self.SOURCES:
            raise ValueError('유효한 현재 위치 측정값이 필요해요.')
        try:
            timestamp = datetime.fromisoformat(payload.get('updated_at', '').replace('Z', '+00:00'))
            if timestamp.tzinfo is None: raise ValueError()
            age = (self._utc_now() - timestamp).total_seconds()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('위치 측정 시간을 확인해 주세요.') from None
        if not -5 <= age <= 30:
            raise ValueError('최신 위치를 받지 못했어요. 잠시 후 다시 시도해 주세요.')
        with self._lock:
            if self._position:
                previous_timestamp = datetime.fromisoformat(self._position['updated_at'])
                if timestamp < previous_timestamp:
                    raise ValueError('이전 위치 측정값은 사용하지 않았어요.')
                if timestamp == previous_timestamp:
                    # A retry is the same measurement, even after wall-clock
                    # correction. Never reset its monotonic age or contents.
                    return self.public()
            self._position = {'latitude': latitude, 'longitude': longitude,
                              'accuracy_m': accuracy, 'source': source,
                              'updated_at': timestamp.astimezone(timezone.utc).isoformat()}
            if confidence is not None: self._position['confidence'] = confidence
            self._received = self._monotonic()
            self._initial_age = max(age, 0)
            self._invalid_status = None
        return self.public()

    def _age(self):
        return self._initial_age + max(0, self._monotonic() - self._received)

    def public(self):
        with self._lock:
            if self._position is None or self._invalid_status:
                messages = {
                    'permission_required':'위치 접근을 허용하면 주변 장소를 찾을 수 있어요.',
                    'unsupported':'현재 위치를 사용할 수 없는 환경이에요. 검색할 지역을 직접 정해 주세요.',
                    'error':'현재 위치를 받지 못했어요. 잠시 후 다시 시도하거나 지역을 직접 정해 주세요.',
                }
                return {'available': False, 'status': self._invalid_status or 'waiting',
                        'message': messages.get(self._invalid_status, '현재 위치를 확인하고 있어요. 지역을 직접 정할 수도 있어요.')}
            age = self._age()
            available = age <= self.MAX_AGE
            return {'available': available, 'accuracy_m': round(self._position['accuracy_m']) if self._position['accuracy_m'] is not None else None,
                    'source': self._position['source'], 'updated_at': self._position['updated_at'],
                    'age_seconds': round(age),
                    'message': '현재 위치 주변에서 찾아요.' if available else '현재 위치를 갱신하고 있어요. 잠시 후 다시 시도하거나 지역을 직접 정해 주세요.'}

    def for_search(self):
        with self._lock:
            if self._position is None or self._invalid_status or self._age() > self.MAX_AGE:
                raise LocationUnavailable('현재 위치를 확인하지 못했어요. 잠시 후 다시 시도하거나 지역을 직접 정해 주세요.')
            return dict(self._position)


class VehiclePosition(HostPosition):
    """Pleos FusedLocation confidence is not a distance accuracy measurement."""
    SOURCES = {'vehicle'}
    REQUIRE_ACCURACY = False
    STATUSES = {'unsupported', 'permission_required', 'unavailable', 'error', 'stopped'}

    def update(self, payload):
        if isinstance(payload, dict) and payload.get('available') is False:
            status = payload.get('status')
            if status not in self.STATUSES:
                raise ValueError('위치 연결 상태를 확인해 주세요.')
            with self._lock:
                # Retain the timestamp watermark: replaying an old fix must not
                # reactivate location after permission loss or lifecycle stop.
                self._invalid_status = status
                return self.public()
        return super().update(payload)
