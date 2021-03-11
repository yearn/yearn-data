import * as AWS from "aws-sdk";
import { ExpressionAttributeNameMap, Key } from "aws-sdk/clients/dynamodb";
import fromEntries from "fromentries";

const client = new AWS.DynamoDB.DocumentClient();

function hash(string: string): number {
  let hash = 0;
  if (string.length === 0) return hash;
  for (let i = 0; i < string.length; i++) {
    const chr = string.charCodeAt(i);
    hash = (hash << 5) - hash + chr;
    hash |= 0; // Convert to 32bit integer
  }
  return Math.abs(hash);
}

function chunk<T>(input: T[], size: number): T[][] {
  return input.reduce((arr, item, idx) => {
    return idx % size === 0
      ? [...arr, [item]]
      : [...arr.slice(0, -1), [...arr.slice(-1)[0], item]];
  }, []);
}

export function parseProjection(
  attributes?: string[]
): [string | undefined, ExpressionAttributeNameMap | undefined] {
  if (!attributes) return [undefined, undefined];
  const names = attributes.map((attr) => attr.split(".")).flat();
  const map = fromEntries(names.map((name) => [`#${hash(name)}`, name]));
  let expr = attributes.join(", ");
  console.log(expr);
  for (const [sub, name] of Object.entries(map)) {
    expr = expr.split(name).join(sub);
  }
  console.log(expr);
  return [expr, map];
}

export async function batchGet<T>(
  table: string,
  keys: Record<string, string>[],
  batchSize = 50
): Promise<T[]> {
  return (
    await Promise.all(
      chunk(keys, batchSize).map(async (Keys) => {
        const params = {
          RequestItems: {
            [table]: {
              Keys,
            },
          },
        };
        const { Responses: res } = await client.batchGet(params).promise();
        if (!res) return [];
        return res[table];
      })
    )
  ).flat() as T[];
}

export async function batchSet<T>(
  table: string,
  items: T[],
  batchSize = 5
): Promise<void> {
  await Promise.all(
    chunk(items, batchSize).map(async (chunk) => {
      const params = {
        RequestItems: {
          [table]: chunk.map((Item) => ({
            PutRequest: { Item },
          })),
        },
      };
      try {
        await client.batchWrite(params).promise();
      } catch (err) {
        console.log(JSON.stringify(chunk), err);
      }
    })
  );
}

export async function scan<T>(
  table: string,
  expression?: string[]
): Promise<T[]> {
  const [projection, map] = parseProjection(expression);
  const { Items: items } = await client
    .scan({
      TableName: table,
      ProjectionExpression: projection,
      ExpressionAttributeNames: map,
    })
    .promise();
  return items as T[];
}

export async function get<T>(
  table: string,
  key: Record<string, string>,
  expression?: string[]
): Promise<T> {
  const [projection, map] = parseProjection(expression);
  const { Item: item } = await client
    .get({
      TableName: table,
      Key: key,
      ProjectionExpression: projection,
      ExpressionAttributeNames: map,
    })
    .promise();
  return item as T;
}

export async function remove(table: string, key: Key): Promise<void> {
  await client.delete({ Key: key, TableName: table }).promise();
}
