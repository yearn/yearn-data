import { VaultV1Contract__factory } from "lib/contracts/index";
import { Context } from "lib/data/context";
import { Apy, calculateFromPps } from "lib/protocols/common/apy";
import { estimateBlockPrecise, fetchLatestBlock } from "lib/utils/block";
import { seconds } from "lib/utils/time";

import { VaultV1 } from "../../interfaces";
import { fetchInceptionBlock } from "../../reader";

export async function calculateSimple(
  vault: VaultV1,
  ctx: Context
): Promise<Apy> {
  const contract = VaultV1Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  const inception = await fetchInceptionBlock(vault, ctx);
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

  const netApy =
    Math.max(ppsSampleData.oneMonthSample, ppsSampleData.oneWeekSample) || 0;

  const totalPerformanceFee =
    (vault.strategistReward + vault.treasuryFee + vault.performanceFee) / 10000;
  const withdrawalFee = vault.withdrawalFee / 1000;

  const grossApy = netApy / (1 - totalPerformanceFee);

  const data = {
    ...ppsSampleData,
    grossApy,
    netApy,
    withdrawalFee,
    performanceFee: totalPerformanceFee,
  };

  const apy = {
    recommended: data.oneMonthSample || 0,
    type: "pricePerShareV1OneMonth",
    composite: false,
    description: "Price per share - One month sample",
    data,
  };

  return apy;
}
