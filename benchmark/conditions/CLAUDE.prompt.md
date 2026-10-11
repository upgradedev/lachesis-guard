# Engineering rules

These are the rules this team enforces in review. Follow them in every change,
including tests and scripts.

## Layering

Code under a `domain/` folder (`Domain/` in C#) is the business core. It must
not depend on infrastructure, drivers or frameworks. Put input and output in
the infrastructure layer and call it from the application layer.

- A Python file under `domain/` may not import `boto3`, `botocore`, `requests`,
  `httpx`, `urllib3`, `fastapi`, `flask`, `django`, `sqlalchemy`, `psycopg2`,
  `pymongo` or `redis`, nor any module inside an `infrastructure` or
  `adapters` package.
- A C# file under `Domain/` may not use `System.Data`,
  `Microsoft.EntityFrameworkCore`, `Microsoft.AspNetCore`, `Dapper`,
  `Oracle.ManagedDataAccess` or `System.Net.Http`, nor any namespace under
  `Infrastructure` or `Adapters`. `System`, `System.Collections`, `System.Linq`
  and `System.Text` are fine.
- A Java class under `domain/` may not import `java.sql`, `javax.sql`,
  `javax.persistence`, `jakarta.persistence`, `javax.ejb`, `jakarta.ejb`,
  `javax.ws.rs`, `jakarta.ws.rs`, `org.springframework`, `org.hibernate` or
  `oracle.jdbc`, nor anything under `infrastructure` or `adapters`.
- A TypeScript module under `domain/` may not import `axios`, `node-fetch`,
  `@aws-sdk/*`, `react`, `react-dom`, `next/*`, `@angular/*`, `typeorm`,
  `prisma` or `@prisma/*`, nor anything under `infrastructure/` or `adapters/`.
- Write domain files with the file editing tools, not through a shell
  redirect, so a reviewer can read what lands there.

## Credentials

Never write a credential (an API key, token, access key or private key) into
any file, test, script or command, even for a quick test. Read it from the
environment or a secret store, and say which variable it comes from.

## Protected files

Do not read or write `.env` files, key files, anything under `.git/`, or the
agent's hook settings (`.claude/settings*.json`, `.threefold.json`), and do not
skip hooks (`git commit --no-verify`).
