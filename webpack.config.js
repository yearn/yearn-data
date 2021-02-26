const path = require("path");
const slsw = require("serverless-webpack");

const TsconfigPathsPlugin = require("tsconfig-paths-webpack-plugin");
const NodeExternals = require("webpack-node-externals");

module.exports = {
  context: __dirname,
  mode: slsw.lib.webpack.isLocal ? "development" : "production",
  entry: slsw.lib.entries,
  devtool: slsw.lib.webpack.isLocal
    ? "eval-cheap-module-source-map"
    : "source-map",
  resolve: {
    extensions: [".mjs", ".json", ".ts"],
    symlinks: false,
    cacheWithContext: false,
    plugins: [
      new TsconfigPathsPlugin({
        configFile: "./tsconfig.paths.json",
      }),
    ],
  },
  output: {
    libraryTarget: "commonjs",
    path: path.join(__dirname, ".webpack"),
    filename: "[name].js",
  },
  optimization: {
    concatenateModules: false,
  },
  target: "node",
  externals: [NodeExternals()],
  module: {
    rules: [
      {
        test: /\.(tsx?)$/,
        loader: "ts-loader",
        exclude: [
          [
            path.resolve(__dirname, "node_modules"),
            path.resolve(__dirname, ".serverless"),
            path.resolve(__dirname, ".webpack"),
          ],
        ],
        options: {
          transpileOnly: true,
          experimentalWatchApi: true,
        },
      },
    ],
  },
  plugins: [],
};
