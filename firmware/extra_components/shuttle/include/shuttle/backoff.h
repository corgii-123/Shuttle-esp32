// Exponential backoff with jitter, shared by every Shuttle reconnect loop.
#pragma once

#include <esp_random.h>

#include <algorithm>
#include <cstdint>

namespace shuttle {

class Backoff {
public:
    Backoff(int min_ms, int max_ms) : min_ms_(std::max(1, min_ms)), max_ms_(std::max(min_ms, max_ms)) {
        Reset();
    }

    // Returns the delay to wait before the next attempt and doubles the base delay.
    // Jitter: +/- 20% so many devices (or both channels) do not retry in lockstep.
    int Next() {
        int base = current_ms_;
        current_ms_ = std::min(max_ms_, current_ms_ * 2);
        int jitter = base / 5;
        if (jitter > 0) {
            base += static_cast<int>(esp_random() % (2 * jitter + 1)) - jitter;
        }
        attempts_++;
        return std::max(min_ms_ / 2, base);
    }

    void Reset() {
        current_ms_ = min_ms_;
        attempts_ = 0;
    }

    int attempts() const { return attempts_; }

private:
    int min_ms_;
    int max_ms_;
    int current_ms_ = 0;
    int attempts_ = 0;
};

}  // namespace shuttle
