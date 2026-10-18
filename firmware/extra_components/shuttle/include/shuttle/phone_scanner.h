// Finds phones on the local Wi-Fi that run adbd in TCP mode (`adb tcpip 5555`).
//
// The board does not need to know any IP: it tries TCP <port> on every address of its /24
// (a few in parallel, non-blocking), and for every open port checks that it really is adbd by
// sending an ADB CNXN packet and expecting CNXN/AUTH/STLS back. That probe never shows a
// prompt on the phone (the "Allow USB debugging?" dialog only appears for a host that sends
// its public key) and the connection is closed right away.
//
// On a phone hotspot the hotspot phone is the gateway (checked first); on a router any
// number of phones can answer. The server decides which one is used (docs/protocol.md).
#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace shuttle {

class PhoneScanner {
public:
    struct Options {
        int port = 5555;
        int batch = 8;               // parallel connects (stay below the ARP table size)
        int connect_timeout_ms = 700;
        int probe_timeout_ms = 800;  // wait for the adbd answer to CNXN
    };

    static Options DefaultOptions();  // from Kconfig

    explicit PhoneScanner(Options options) : options_(options) {}

    // Scans the /24 (or smaller subnet) around `ip` (all values network byte order).
    // `first` hosts are tried first and keep their order in the result; `in_use` (the phone
    // the bridge is spliced to right now) only needs an open port, it is not probed again.
    // Stops early (returns what it has) when `abort` becomes true.
    std::vector<std::string> Scan(uint32_t ip, uint32_t netmask, const std::vector<std::string>& first,
                                  const std::string& in_use, const std::atomic<bool>& abort);

    // Single host: is adbd listening there?
    bool IsAdbd(const std::string& host);

private:
    // Non-blocking connect to each address; returns the connected sockets (caller closes).
    void ConnectBatch(const std::vector<uint32_t>& addrs, std::vector<std::pair<uint32_t, int>>* open);
    bool Probe(int sock);

    Options options_;
};

}  // namespace shuttle
