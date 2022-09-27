function FindProxyForURL(url, host) {
    if (shExpMatch(host, "*.diamond.ac.uk") ||
        shExpMatch(host, "*.rl.ac.uk") ||
        isInNet(host, "172.23.0.0", "255.255.0.0") ||
        shExpMatch(host, "*.cclrc.ac.uk")
    ) {
        return "SOCKS5 localhost:9090";
    } else {
        return "DIRECT";
    }
}
