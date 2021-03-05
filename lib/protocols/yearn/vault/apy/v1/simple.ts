import { VaultV1Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { Apy, calculateFromPps } from "@protocols/common/apy";
import { estimateBlockPrecise, fetchLatestBlock } from "@utils/block";
import { seconds } from "@utils/time";

import { VaultV1 } from "../../interfaces";
import { fetchInceptionBlock } from "../../reader";

export async function calculateSimpleApy(
  vault: VaultV1,
  ctx: Context
): Promise<Apy> {
  const contract = VaultV1Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  const inception = await fetchInceptionBlock(vault.address, ctx);
  if (!inception) {
    return {
      recommended: 0,
      composite: false,
      type: "error",
      description: "no inception sampple",
      data: { oneMonthSample: null, inceptionSample: null },
    };
  }
  const latest = await fetchLatestBlock(ctx);
  const oneWeek = await estimateBlockPrecise(
    latest.timestamp - seconds("1 week"),
    ctx
  );
  const oneMonth = await estimateBlockPrecise(
    latest.timestamp - seconds("4 weeks"),
    ctx
  );

  const ppsSampleData = await calculateFromPps(
    latest.block,
    inception.block,
    {
      oneWeekSample: oneWeek,
      oneMonthSample: oneMonth,
      inceptionSample: inception.block,
    },
    contract.getPricePerFullShare
  );

  const netApy = Math.max(
    ppsSampleData.oneMonthSample ?? 0,
    ppsSampleData.oneWeekSample ?? 0
  );

  const totalPerformanceFee = vault.fees.general.performanceFee / 1e4;

  const withdrawalFee = vault.fees.general.withdrawalFee / 1e4;

  const grossApy = netApy / (1 - totalPerformanceFee);

  const data = {
    ...ppsSampleData,
    grossApy,
    netApy,
    withdrawalFee,
  };

  const apy = {
    recommended: data.grossApy,
    type: "pricePerShareV1OneMonth",
    composite: false,
    description: "Price per share - One month sample",
    data,
  };

  return apy;
}
