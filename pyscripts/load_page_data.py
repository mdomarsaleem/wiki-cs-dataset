import csv
import os
import json
import nltk

try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

def page_titles_to_ids(titles_set, page_table_filename):
    mapping = {}
    with open(page_table_filename, encoding='utf8') as page_file:
        reader = csv.reader(page_file)
        for line in reader:
            id, namespace, title = int(line[0]), line[1], line[2]
            if namespace == '0' and title in titles_set:
                mapping[title] = id
    return mapping


def page_titles_to_labels(category_label_mapping, page2cat_filename):
    category_to_label = {}
    for (name, category_list) in category_label_mapping.items():
        for category in category_list:
            if category not in category_to_label:
                category_to_label[category] = []
            category_to_label[category].append(name)

    titles_to_labels = {}
    with open(page2cat_filename, encoding='utf8') as page2cat_file:
        reader = csv.reader(page2cat_file, delimiter='\t')
        for line in reader:
            title = line[0].replace(" ", "_")
            label_lists = [category_to_label[line[i]] \
                        for i in range(1, len(line)) \
                        if line[i] in category_to_label]
            labels = {label for one_list in label_lists for label in one_list}
            if len(labels) > 0:
                titles_to_labels[title] = labels

    return titles_to_labels


def load_redirects(page_table_filename, redirect_table_filename):
    target_title_to_source_id = {}
    with open(redirect_table_filename, encoding='utf8') as redirect_file:
        reader = csv.reader(redirect_file)
        for from_id, to_namespace, to_title, _, _ in reader:
            from_id = int(from_id)
            if to_namespace == '0':
                if to_title not in target_title_to_source_id:
                    target_title_to_source_id[to_title] = []
                target_title_to_source_id[to_title].append(from_id)

    source_to_target_id = {}
    with open(page_table_filename, encoding='utf8') as page_file:
        reader = csv.reader(page_file)
        for line in reader:
            id, title, is_redirect = int(line[0]), line[2], line[5]
            if is_redirect == '0': # Ignore double redirects
                for source_id in target_title_to_source_id.get(title, []):
                    source_to_target_id[source_id] = id

    return source_to_target_id


def links_between_pages(page_id_set, pagelinks_table_filename, page_table_filename, redirect_table_filename):
    titles_linked_from = {}
    with open(pagelinks_table_filename, encoding='utf8') as pagelinks_file:
        reader = csv.reader(pagelinks_file)
        for from_id, from_namespace, to_title, to_namespace in reader:
            from_id = int(from_id)
            if from_id in page_id_set and from_namespace == '0' and to_namespace == '0':
                if to_title not in titles_linked_from:
                    titles_linked_from[to_title] = []
                titles_linked_from[to_title].append(from_id)

    redirects = load_redirects(page_table_filename, redirect_table_filename)

    links = {id: [] for id in page_id_set}
    with open(page_table_filename, encoding='utf8') as page_file:
        reader = csv.reader(page_file)
        for line in reader:
            id, title, is_redirect = int(line[0]), line[2], line[5]
            if is_redirect == '1':
                if id in redirects:
                    id = redirects[id]
                else:
                    continue

            for source_id in titles_linked_from.get(title, []):
                links[source_id].append(id)

    return links


def filter_for_main_namespace(input_filename, output_filename, field_indices):
    with open(input_filename, encoding='utf8') as input_file, \
         open(output_filename, mode='w+', encoding='utf8', newline='') as output_file:
        reader = csv.reader(input_file)
        writer = csv.writer(output_file, quoting=csv.QUOTE_MINIMAL)
        for row in reader:
            if all(row[idx] == '0' for idx in field_indices):
                writer.writerow(row)


def get_text_tokens(page_id_set, text_extractor_data_dir):
    ids_to_tokens = {}
    for root, dirs, files in os.walk(text_extractor_data_dir):
        for file in files:
            for line in open(os.path.join(root, file), "r", encoding='utf8'):
                entry = json.loads(line)
                id = int(entry['id'])
                if id in page_id_set:
                    ids_to_tokens[id] = nltk.word_tokenize(entry['text'])
    return ids_to_tokens


class Node:
    def __init__(self, id, labels, title, outlinks, tokens):
        self.id = id
        self.title = title
        self.outlinks = outlinks
        self.tokens = tokens
        self.labels = labels


def load_with_multiple_label_maps(label_mapping_list, page2cat_filename, page_table_filename, pagelinks_table_filename, redirect_table_filename, text_extractor_data):
    # Get titles to mapped to labels for each label dataset
    multi_titles_to_labels = [page_titles_to_labels(label_mapping, page2cat_filename) \
                                for label_mapping in label_mapping_list]
    # Get titles and map them to page IDs
    all_titles = set([]).union(*[titles_to_labels.keys() for titles_to_labels in multi_titles_to_labels])
    all_titles_to_ids = page_titles_to_ids(all_titles, page_table_filename)
    all_ids_to_titles = {v:k for (k,v) in all_titles_to_ids.items()}
    all_ids = set(all_titles_to_ids.values())

    # Load link and text data
    all_links = links_between_pages(all_ids, pagelinks_table_filename, page_table_filename, redirect_table_filename)
    all_ids_to_tokens = get_text_tokens(all_ids, text_extractor_data)

    all_valid_links = {source: [target for target in outlinks \
                                if (target in all_links and target in all_ids_to_tokens)] \
                        for (source, outlinks) in all_links.items()}

    # Get ID sets of valid pages with no data missing for each individual dataset
    id_sets = [{all_titles_to_ids[title] for title in titles_to_labels.keys() \
                    if (title in all_titles_to_ids \
                    and all_titles_to_ids[title] in all_valid_links \
                    and all_titles_to_ids[title] in all_ids_to_tokens)} \
                for titles_to_labels in multi_titles_to_labels]

    # Create datasets as a list of sets of Node objects
    result = [{ \
        id: Node(id, all_ids_to_titles[id], multi_titles_to_labels[i][all_ids_to_titles[id]], \
                 [out_id for out_id in all_valid_links[id] if out_id in id_sets[i]], all_ids_to_tokens[id]) \
        for id in id_sets[i] \
    } for i in range(len(id_sets))]
    return result


def load_single_dataset(label_mapping, page2cat_filename, page_table_filename, pagelinks_table_filename, redirect_table_filename, text_extractor_data):
    return load_with_multiple_label_maps([label_mapping], page2cat_filename, page_table_filename, pagelinks_table_filename, redirect_table_filename, text_extractor_data)
