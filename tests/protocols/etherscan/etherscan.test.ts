import "dotenv/config";

import { Context } from "@data/context";
import { fetchTransactionList } from "@protocols/etherscan";

describe("etherscan integration", () => {
  let ctx: Context;

  beforeAll(() => {
    ctx = new Context({ etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("should fetch tx list (network)", () => {
    const address = "0x5f18C75AbDAe578b483E5F43f12a39cF75b973a9";
    const request = fetchTransactionList({ address, offset: 1 }, ctx);
    return expect(request).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          timeStamp: expect.any(Number),
          blockNumber: expect.any(Number),
        }),
      ])
    );
  }, 3e4);
});
