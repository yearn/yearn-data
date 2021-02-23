import { VaultV1, VaultV2 } from "@protocols/yearn/vault";

export const vaults = {
  v1: {
    address: "0x5dbcF33D8c2E976c6b560249878e6F1491Bca25c",
    object: ({
      address: "0x5dbcF33D8c2E976c6b560249878e6F1491Bca25c",
      name: "yearn Curve.fi yDAI/yUSDC/yUSDT/yTUSD",
      symbol: "yyDAI+yUSDC+yUSDT+yTUSD",
      decimals: 18,
      token: {
        name: "Curve.fi yDAI/yUSDC/yUSDT/yTUSD",
        symbol: "yDAI+yUSDC+yUSDT+yTUSD",
        address: "0xdF5e0e81Dff6FAF3A7e52BA697820c5e32D806A8",
        decimals: 18,
      },
      type: "v1",
    } as unknown) as VaultV1,
    inception: 10559471,
  },
  v2: {
    address: "0x33bd0f9618cf38fea8f7f01e1514ab63b9bde64b",
    object: ({
      address: "0x33bd0f9618cf38fea8f7f01e1514ab63b9bde64b",
      name: "yearn USD Coin IdleStrategies Test",
      symbol: "yUSDCIdleTest",
      emergencyShutdown: false,
      decimals: 6,
      tags: [],
      token: {
        name: "USD Coin",
        symbol: "USDC",
        address: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        decimals: 6,
      },
      apiVersion: "0.2.2",
      type: "v2",
    } as unknown) as VaultV2,
    inception: 11421976,
  },
};
