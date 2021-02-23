import "dotenv/config";

import { Context } from "@data/context";
import { WebSocketProvider } from "@ethersproject/providers";
import { registry } from "@protocols/yearn/vault";

describe("registry", () => {
  let provider: WebSocketProvider;
  let ctx: Context;

  beforeAll(() => {
    provider = new WebSocketProvider(process.env.WEB3_PROVIDER_WSS ?? "");
    ctx = new Context({ provider, etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("should fetch a list of v1 addresses (network)", () => {
    const length = registry.fetchV1Addresses(ctx).then(({ length }) => length);
    return expect(length).resolves.toBeGreaterThan(0);
  }, 1e4);

  it("should fetch a list of v2 addresses (network)", () => {
    const length = registry.fetchV2Addresses(ctx).then(({ length }) => length);
    return expect(length).resolves.toBeGreaterThan(0);
  }, 1e4);

  it("should fetch a list of v2 experimental addresses (network)", () => {
    const length = registry
      .fetchV2ExperimentalAddresses(ctx)
      .then(({ length }) => length);
    return expect(length).resolves.toBeGreaterThan(0);
  }, 1e4);

  afterAll(() => {
    return provider.destroy();
  });
});
