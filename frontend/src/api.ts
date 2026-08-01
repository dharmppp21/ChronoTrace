import createClient from "openapi-fetch";
import type { paths } from "./api-schema";

const client = createClient<paths>({ baseUrl: "/" });

export default client;
