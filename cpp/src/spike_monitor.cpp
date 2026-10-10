// Passive IIR monitor. No sendSequence, DAC, routing, or stimulation calls.
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <thread>
#include "maxlab/maxlab.h"

namespace { volatile std::sig_atomic_t stopped = 0; void stop(int) { stopped = 1; } }

int main(int argc, char** argv) {
    const int well = argc > 1 ? std::atoi(argv[1]) : 0;
    if (well < 0 || well > 255) return 2;
    std::signal(SIGINT, stop);
    std::signal(SIGTERM, stop);
    maxlab::checkVersions();
    auto status = maxlab::DataStreamerFiltered_open(maxlab::FilterType::IIR);
    if (status != maxlab::Status::MAXLAB_OK) {
        std::cerr << "open status=" << static_cast<int>(status) << '\n';
        return 3;
    }
    std::cout << "{\"state\":\"ready\"}" << std::endl;
    std::uint64_t spikes = 0, frames = 0, corrupt = 0, no_frame = 0;
    auto last = std::chrono::steady_clock::now();
    int result = 0;
    while (!stopped) {
        maxlab::FilteredFrameData frame{};
        status = maxlab::DataStreamerFiltered_receiveNextFrame(&frame);
        if (status == maxlab::Status::MAXLAB_NO_FRAME) {
            ++no_frame;
            continue;
        }
        if (status != maxlab::Status::MAXLAB_OK) { result = 4; break; }
        if (frame.frameInfo.well_id != well) continue;
        if (frame.frameInfo.corrupted) { ++corrupt; continue; }
        ++frames;
        spikes += frame.spikeCount;
        auto now = std::chrono::steady_clock::now();
        if (now - last >= std::chrono::seconds(1)) {
            std::cout << "{\"frame\":" << frame.frameInfo.frame_number << ",\"frames\":" << frames
                      << ",\"spikes\":" << spikes << ",\"corrupt\":" << corrupt
                      << ",\"no_frame\":" << no_frame << "}" << std::endl;
            last = now;
        }
    }
    status = maxlab::DataStreamerFiltered_close();
    if (status != maxlab::Status::MAXLAB_OK) {
        std::cerr << "close status=" << static_cast<int>(status) << '\n';
        return 5;
    }
    return result;
}
