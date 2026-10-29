"""Diagnostics without SQLAlchemy statements, bound values or credentials."""
import re
import secrets

MYSQL_HINTS = {
    229: ('permissão de origem', 'Confira SELECT e VIEW DEFINITION para o usuário no banco de origem.'),
    18456: ('autenticação de origem', 'Confira autenticação SQL Server, usuário e senha.'),
    1044: ('permissão', 'Conceda acesso ao banco de destino.'),
    1045: ('autenticação', 'Confira usuário, senha e host autorizado.'),
    1050: ('objeto existente', 'Atualize o plano ou escolha pular tabelas existentes.'),
    1061: ('índice existente', 'O nome do índice já está em uso.'),
    1062: ('dados incompatíveis', 'Há valores duplicados para uma chave UNIQUE ou primária.'),
    1064: ('DDL incompatível', 'Revise a sintaxe e a versão do MySQL.'),
    1071: ('limite de índice', 'A chave excede o limite em bytes do MySQL. Revise tipo, tamanho e charset.'),
    1118: ('limite de linha', 'A linha excede o limite do InnoDB. Considere TEXT/BLOB ou reduzir VARCHARs.'),
    1142: ('permissão', 'Conceda CREATE, INSERT, ALTER, INDEX, SELECT e REFERENCES conforme a operação.'),
    1170: ('índice incompatível', 'TEXT/BLOB exige prefixo de índice. Revise o tipo ou adapte o índice manualmente.'),
    1215: ('relacionamento incompatível', 'Confira tipos, índices, collation e tabela referenciada.'),
    1264: ('valor fora do intervalo', 'Revise o tamanho e o sinal do tipo numérico.'),
    1265: ('truncamento', 'O tipo de destino não representa o valor sem alteração.'),
    1292: ('valor incompatível', 'Confira datas, horas e valores numéricos aceitos pelo MySQL.'),
    1366: ('valor ou charset incompatível', 'Confira os tipos e a codificação dos valores de origem.'),
    1406: ('texto maior que o destino', 'Aumente o tamanho do tipo de destino antes de repetir.'),
    1452: ('integridade referencial', 'Há registros sem correspondente na tabela referenciada.'),
    1822: ('índice referenciado ausente', 'Crie ou corrija a chave referenciada antes de adicionar a FK.'),
    3780: ('tipos de FK incompatíveis', 'Os dois lados da FK precisam de tipos, sinal e collation compatíveis.'),
    2003: ('conexão indisponível', 'Confira host, porta e rede.'),
    2006: ('conexão perdida', 'Confira timeout e max_allowed_packet; valide a tabela antes de repetir.'),
    2013: ('conexão perdida', 'O resultado do commit pode ser incerto. Inspecione o destino antes de repetir.'),
}


def diagnostic(exc, stage, object_name=None, engines=(), hide_values=False):
    original = getattr(exc, 'orig', exc)
    args = getattr(original, 'args', ())
    code = args[0] if args and isinstance(args[0], int) else None
    message = ' | '.join(a.decode('utf-8', errors='replace') if isinstance(a, bytes) else str(a) for a in args) if args else str(original)
    # DBAPIError.__str__ would append SQL/parameters; only use .orig above.
    message = message.split('[SQL:')[0].split('[parameters:')[0]
    for engine in engines:
        url = getattr(engine, 'url', None)
        for secret in (getattr(url, 'password', None), getattr(url, 'username', None)):
            if secret: message = message.replace(str(secret), '[oculto]')
    message = re.sub(r'(?i)(?:mssql|mysql)(?:\+\w+)?://[^\s]+|https?://[^\s]+', '[conexão oculta]', message)
    message = re.sub(r'(?i)((?:password|pwd|senha)\s*[=:]\s*)([^;\s]+)', r'\1[oculto]', message)
    if hide_values:
        # MySQL embeds offending row values in quotes in duplicate/truncation errors.
        message = re.sub(r"'[^']*'|\"[^\"]*\"", '[valor oculto]', message)
        if code in (1062, 1264, 1265, 1292, 1366, 1406):
            message = 'O servidor rejeitou valores da carga; conteúdo dos registros omitido.'
    category, hint = MYSQL_HINTS.get(code, ('erro de execução', 'Confira o objeto, a etapa e a mensagem; corrija a causa antes de repetir.'))
    if type(exc).__name__ in ('CompileError', 'UnsupportedCompilationError'):
        category, hint = 'DDL incompatível', 'Revise os tipos e a definição do objeto para o dialeto MySQL.'
    if isinstance(exc, NotImplementedError):
        category, hint = 'metadado não suportado', 'O driver não implementa esta reflexão de metadados.'
    return {'reference': secrets.token_hex(6), 'stage': stage, 'object': object_name,
            'exception': type(exc).__name__, 'driver_exception': type(original).__name__,
            'code': code, 'category': category, 'hint': hint, 'message': message[:3000] or 'Exceção sem mensagem.'}
