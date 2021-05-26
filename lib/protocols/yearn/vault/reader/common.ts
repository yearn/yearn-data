import { Context } from "@data/context";
import { fetchTransactionList } from "@protocols/etherscan";
import { TimedBlock } from "@utils/block";

export async function fetchInceptionBlock(address: string, ctx: Context): Promise<TimedBlock | null> {
  const txList = await fetchTransactionList({ address: address, page: 1, offset: 3 }, ctx);
  if (txList.length < 3) {
    return null;
  }
  const inception = txList[2]; // skip contract creation
  return { block: inception.blockNumber, timestamp: inception.timeStamp };
}
